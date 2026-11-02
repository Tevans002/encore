"""
Live layer: API contract tests for every endpoint the tools in Part 3 call.

    python -m pytest tests/live/test_live_endpoints.py     # about 12 real requests, under a minute

You write these tools yourself, so these tests don't call your code. They call
the endpoints directly and check that each one still returns the fields your
tool will depend on. Read them as executable documentation: each test shows the
exact URL, parameters, and response shape for one tool. If one fails, the API
changed, and so must your tool.

All requests are read-only. LB Radio and popularity require a token (LB Radio is an
"expensive endpoint"; popularity was closed to anonymous callers because of scrapers),
so their tests skip unless LISTENBRAINZ_TOKEN is set.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, for helpers.py
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/live/, for live_helpers.py
from helpers import UUID_RE
from live_helpers import http_retry, known_recording

pytestmark = pytest.mark.live

NO_SUCH_USER = "encore-no-such-user-9f3a"
INVENTED_MBID = "00000000-0000-4000-8000-000000000000"


def lb_get(enc, path, auth=False, **params):
    """A ListenBrainz read with the slow-read timeout (LB Radio can take seconds)."""
    return http_retry(enc, "GET", f"{enc.LB_API}{path}", params=params or None, auth=auth,
                      timeout=enc.SLOW_READ_TIMEOUT_S)


# ============================================================================
# listening domain: get_feedback
# ============================================================================

class TestFeedback:
    """get_feedback -> /1/feedback/user/{user}/get-feedback?score=1|-1"""

    def test_loved_recordings_shape(self, live_enc, test_user):
        status, body, _ = lb_get(live_enc, f"/feedback/user/{test_user}/get-feedback", score=1, count=5)
        assert status == 200, f"expected 200, got {status}: {body}"
        assert isinstance(body["feedback"], list) and isinstance(body["total_count"], int)
        assert len(body["feedback"]) <= 5
        for fb in body["feedback"]:
            assert fb["score"] == 1, "score=1 must return only loved recordings"
            assert fb.get("recording_mbid") or fb.get("recording_msid")

    def test_feedback_for_specific_recordings(self, live_enc, test_user):
        """The lookup set_feedback uses to read a recording's current score (0 = none)."""
        mbid = known_recording(live_enc)["recording_mbid"]
        status, body, _ = lb_get(live_enc, f"/feedback/user/{test_user}/get-feedback-for-recordings",
                                 recording_mbids=mbid)
        assert status == 200, f"expected 200, got {status}: {body}"
        assert [fb["recording_mbid"] for fb in body["feedback"]] == [mbid]
        assert body["feedback"][0]["score"] in (-1, 0, 1)


# ============================================================================
# catalog domain: MusicBrainz artist relationships, popularity
# ============================================================================

@pytest.fixture
def token(live_enc):
    if not os.getenv("LISTENBRAINZ_TOKEN"):
        pytest.skip("this endpoint requires a token; set LISTENBRAINZ_TOKEN")
    return True


class TestCatalog:
    def test_artist_lookup_with_relationships(self, live_enc):
        """Artist lookup with inc=artist-rels (members, collaborators): the related-artist hop."""
        artist = known_recording(live_enc)["artist_mbid"]
        status, body, _ = http_retry(live_enc, "GET", f"{live_enc.MB_API}/artist/{artist}",
                                     params={"inc": "artist-rels"})
        assert status == 200, f"expected 200, got {status}"
        assert body["id"] == artist and body["name"]
        assert body["relations"], "Pearl Jam has member relationships"
        for rel in body["relations"]:
            assert rel["type"] and UUID_RE.match(rel["artist"]["id"]) and rel["artist"]["name"]

    def test_popular_recordings_for_artist(self, live_enc, token):
        """/1/popularity/top-recordings-for-artist/{mbid} returns a bare list, not a payload.

        Token required: ListenBrainz began refusing anonymous calls here (HTTP 401) because of scrapers.
        """
        artist = known_recording(live_enc)["artist_mbid"]
        status, body, _ = lb_get(live_enc, f"/popularity/top-recordings-for-artist/{artist}", auth=True)
        assert status == 200, f"expected 200, got {status}: {body}"
        assert isinstance(body, list) and body, "the response is a top-level JSON list"
        for r in body[:10]:
            assert UUID_RE.match(r["recording_mbid"]) and r["recording_name"]
            assert isinstance(r["total_listen_count"], int) and isinstance(r["total_user_count"], int)
        counts = [r["total_listen_count"] for r in body]
        assert counts == sorted(counts, reverse=True), "most-listened first"

    def test_popularity_rejects_a_malformed_mbid(self, live_enc, token):
        status, _, _ = lb_get(live_enc, "/popularity/top-recordings-for-artist/not-an-mbid", auth=True)
        assert status == 400


# ============================================================================
# discovery domain: get_fresh_releases, lb_radio
# ============================================================================

class TestDiscovery:
    def test_fresh_releases_shape(self, live_enc, test_user):
        """get_fresh_releases -> /1/user/{user}/fresh_releases?days= (at most 90)."""
        status, body, _ = lb_get(live_enc, f"/user/{test_user}/fresh_releases", days=30)
        assert status == 200, f"expected 200, got {status}: {body}"
        releases = body["payload"]["releases"]
        assert isinstance(releases, list), "an empty list is fine: no fresh releases from your artists"
        for rel in releases:
            assert rel["release_name"] and rel["artist_credit_name"]
            assert UUID_RE.match(rel["release_mbid"])
            assert len(rel["release_date"]) == 10, "release_date is YYYY-MM-DD"


class TestLBRadio:
    """lb_radio -> /1/explore/lb-radio?prompt=&mode=easy|medium|hard (token required, 5 calls per 5 s)."""

    def test_radio_returns_a_jspf_playlist(self, live_enc, token):
        status, body, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/explore/lb-radio",
                                     params={"prompt": "artist:(Pearl Jam)", "mode": "easy"},
                                     auth=True, timeout=live_enc.SLOW_READ_TIMEOUT_S)
        assert status == 200, f"expected 200, got {status}: {body}"
        tracks = body["payload"]["jspf"]["playlist"]["track"]
        assert tracks, "a radio for a well-known artist should have tracks"
        for t in tracks:
            ident = t["identifier"][0] if isinstance(t["identifier"], list) else t["identifier"]
            assert UUID_RE.match(ident.rstrip("/").rsplit("/", 1)[-1]), "identifiers end in a recording MBID"
            assert t["title"] and t["creator"]
        assert isinstance(body["payload"]["feedback"], list), "radio explains itself here (often empty)"

    def test_bad_prompt_is_an_input_error(self, live_enc, token):
        status, _, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/explore/lb-radio",
                                  params={"prompt": "", "mode": "easy"}, auth=True,
                                  timeout=live_enc.SLOW_READ_TIMEOUT_S)
        assert status == 400 and live_enc.classify_status(status) == "input"


# ============================================================================
# library domain, read side: find_playlists
# ============================================================================

class TestPlaylistsRead:
    """find_playlists lists the user's OWN playlists; with the token, private ones are included.
    Opening a playlist (/1/playlist/{mbid}) is tested in test_live_writes.py on one Encore creates.
    """

    def test_user_playlists_shape(self, live_enc, test_user):
        """/1/user/{user}/playlists: metadata only (no tracks); public ones unless you send your token."""
        status, body, _ = lb_get(live_enc, f"/user/{test_user}/playlists", count=5)
        assert status == 200, f"expected 200, got {status}: {body}"
        assert isinstance(body["playlists"], list) and isinstance(body["playlist_count"], int)
        for item in body["playlists"]:
            pl = item["playlist"]
            assert pl["title"] and UUID_RE.match(pl["identifier"].rstrip("/").rsplit("/", 1)[-1])

    def test_missing_playlist_is_404(self, live_enc):
        status, _, _ = lb_get(live_enc, f"/playlist/{INVENTED_MBID}", fetch_metadata="false")
        assert status == 404


if __name__ == "__main__":
    # Lets you run this file directly (e.g., VS Code's Run button): it hands off to pytest.
    raise SystemExit(pytest.main([__file__, "-v"]))
