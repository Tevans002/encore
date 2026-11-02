"""
Live layer, opt-in: write round trips against YOUR ListenBrainz account.

These are OFF by default. They change your account and then put it back:

    ENCORE_WRITE_TESTS=1 python -m pytest tests/live/test_live_writes.py -v
    (PowerShell:  $env:ENCORE_WRITE_TESTS=1; python -m pytest tests/live/test_live_writes.py -v)

Requires LISTENBRAINZ_TOKEN and LISTENBRAINZ_USER (your own account).

What they do:
  * Playlist round trip: create a PRIVATE playlist titled "[Encore test] ...",
    add a track, remove a track, delete the playlist. The delete runs in a
    `finally`, and any "[Encore test]" playlist left over from an interrupted
    run is deleted first.
  * Feedback round trip: read your current score for one recording (Pearl Jam,
    "Black"), love it, clear it, then restore whatever score you had before.

Export to Spotify or Apple Music is deliberately NOT tested: it creates a real
playlist in your streaming account that this suite can't remove. See the
README's manual check.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, for helpers.py
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/live/, for live_helpers.py
from helpers import UUID_RE
from live_helpers import http_retry, known_recording

pytestmark = pytest.mark.live

TEST_PREFIX = "[Encore test]"
JSPF_EXT = "https://musicbrainz.org/doc/jspf#playlist"


@pytest.fixture
def me(live_enc, monkeypatch):
    """Your own account, with your token. Skips unless writes were explicitly enabled."""
    if os.getenv("ENCORE_WRITE_TESTS") != "1":
        pytest.skip("write tests are opt-in: set ENCORE_WRITE_TESTS=1")
    user, token = os.getenv("LISTENBRAINZ_USER"), os.getenv("LISTENBRAINZ_TOKEN")
    if not (user and token):
        pytest.skip("write tests need LISTENBRAINZ_USER and LISTENBRAINZ_TOKEN")
    monkeypatch.setattr(live_enc, "LB_USER", user)
    monkeypatch.setattr(live_enc, "LB_TOKEN", token)
    status, body, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/validate-token", auth=True)
    assert status == 200 and body.get("valid") and body.get("user_name") == user, \
        "the token must be valid and belong to LISTENBRAINZ_USER before anything is written"
    return user


def write(enc, path, body=None):
    """An authorized ListenBrainz POST with the write timeout."""
    return http_retry(enc, "POST", f"{enc.LB_API}{path}", body=body, auth=True, timeout=enc.WRITE_TIMEOUT_S)


def track(mbid):
    return {"identifier": f"https://musicbrainz.org/recording/{mbid}"}


def fetch_playlist(enc, mbid):
    status, body, _ = http_retry(enc, "GET", f"{enc.LB_API}/playlist/{mbid}", params={"fetch_metadata": "false"},
                                 auth=True, timeout=enc.SLOW_READ_TIMEOUT_S)
    return status, (body["playlist"] if status == 200 else body)


def track_mbids(playlist):
    out = []
    for t in playlist["track"]:
        ident = t["identifier"][0] if isinstance(t["identifier"], list) else t["identifier"]
        out.append(ident.rstrip("/").rsplit("/", 1)[-1])
    return out


def delete_leftovers(enc, user):
    """Remove "[Encore test]" playlists an interrupted earlier run may have left behind."""
    status, body, _ = http_retry(enc, "GET", f"{enc.LB_API}/user/{user}/playlists", params={"count": 100},
                                 auth=True, timeout=enc.SLOW_READ_TIMEOUT_S)
    assert status == 200, f"could not list your playlists (HTTP {status})"
    for item in body["playlists"]:
        pl = item["playlist"]
        if pl["title"].startswith(TEST_PREFIX):
            write(enc, f"/playlist/{pl['identifier'].rstrip('/').rsplit('/', 1)[-1]}/delete")


class TestPlaylistRoundTrip:
    """create_playlist, add_playlist_items, remove_playlist_items, delete_playlist."""

    def test_create_add_remove_delete(self, live_enc, me):
        song = known_recording(live_enc)["recording_mbid"]
        delete_leftovers(live_enc, me)

        new = {"playlist": {"title": f"{TEST_PREFIX} {time.strftime('%Y-%m-%d %H:%M:%S')}",
                            "track": [track(song)],
                            "extension": {JSPF_EXT: {"public": False}}}}
        status, body, _ = write(live_enc, "/playlist/create", new)
        assert status == 200 and body.get("status") == "ok", f"create failed (HTTP {status}): {body}"
        mbid = body["playlist_mbid"]
        assert UUID_RE.match(mbid)
        try:
            status, pl = fetch_playlist(live_enc, mbid)
            assert status == 200, "your token should be able to read your own private playlist"
            assert track_mbids(pl) == [song]
            assert pl["extension"][JSPF_EXT]["public"] is False, "Encore creates private playlists"

            # add: the same JSPF shape, tracks appended at the end
            status, body, _ = write(live_enc, f"/playlist/{mbid}/item/add",
                                    {"playlist": {"track": [track(song)]}})
            assert status == 200, f"add failed (HTTP {status}): {body}"
            assert track_mbids(fetch_playlist(live_enc, mbid)[1]) == [song, song]

            # remove: by position, not by MBID
            status, body, _ = write(live_enc, f"/playlist/{mbid}/item/delete", {"index": 0, "count": 1})
            assert status == 200, f"remove failed (HTTP {status}): {body}"
            assert track_mbids(fetch_playlist(live_enc, mbid)[1]) == [song]

            # the playlist can't be read without the token: private really is private
            status, _, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/playlist/{mbid}",
                                      params={"fetch_metadata": "false"}, timeout=live_enc.SLOW_READ_TIMEOUT_S)
            assert status in (401, 403, 404), f"a private playlist was readable without a token (HTTP {status})"
        finally:
            status, body, _ = write(live_enc, f"/playlist/{mbid}/delete")
            assert status == 200, f"CLEANUP FAILED: delete playlist {mbid} by hand (HTTP {status}): {body}"

        assert fetch_playlist(live_enc, mbid)[0] == 404, "a deleted playlist must be gone"

    def test_deleting_a_missing_playlist_is_404(self, live_enc, me):
        status, _, _ = write(live_enc, "/playlist/00000000-0000-4000-8000-000000000000/delete")
        assert status == 404 and live_enc.classify_status(status) == "resource"


class TestFeedbackRoundTrip:
    """set_feedback: love (1), hate (-1), and clear (0) all go to one endpoint."""

    def score_of(self, enc, user, mbid):
        status, body, _ = http_retry(enc, "GET", f"{enc.LB_API}/feedback/user/{user}/get-feedback-for-recordings",
                                     params={"recording_mbids": mbid}, timeout=enc.SLOW_READ_TIMEOUT_S)
        assert status == 200, f"could not read feedback (HTTP {status}): {body}"
        return body["feedback"][0]["score"]

    def test_love_then_clear_then_restore(self, live_enc, me):
        song = known_recording(live_enc)["recording_mbid"]
        before = self.score_of(live_enc, me, song)
        try:
            for score in (1, 0):
                status, body, _ = write(live_enc, "/feedback/recording-feedback",
                                        {"recording_mbid": song, "score": score})
                assert status == 200 and body.get("status") == "ok", f"score {score} failed (HTTP {status}): {body}"
                assert self.score_of(live_enc, me, song) == score
        finally:
            status, _, _ = write(live_enc, "/feedback/recording-feedback", {"recording_mbid": song, "score": before})
            assert status == 200, f"CLEANUP FAILED: set your feedback for {song} back to {before} by hand"
        assert self.score_of(live_enc, me, song) == before, "your original feedback must be restored"

    def test_invalid_score_is_an_input_error(self, live_enc, me):
        """Why score is an enum in your schema."""
        status, _, _ = write(live_enc, "/feedback/recording-feedback",
                             {"recording_mbid": known_recording(live_enc)["recording_mbid"], "score": 5})
        assert status == 400 and live_enc.classify_status(status) == "input"


if __name__ == "__main__":
    # Lets you run this file directly (e.g., VS Code's Run button): it hands off to pytest.
    raise SystemExit(pytest.main([__file__, "-v"]))
