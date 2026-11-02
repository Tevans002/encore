"""
Live layer: the provided tools against the real ListenBrainz and MusicBrainz APIs.

    python -m pytest tests/live/test_live_apis.py     # about 15 real requests, roughly 30 seconds

Transient failures (timeouts, 429, 5xx) are retried twice with a delay, the way
the agent loop should; a test fails only if the problem persists.

Two kinds of test:
  * API contract tests check that the services still return the fields the
    tools depend on. If one fails, the API changed and the tool needs updating.
  * Tool tests run the real tools end to end and check the ToolResult they return.

Assertions are about shape and invariants (types, ordering, formats), never
about specific songs, because the test user keeps listening to new music.
All requests are read-only.
"""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, for helpers.py
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/live/, for live_helpers.py
from helpers import UUID_RE, parse
from live_helpers import http_retry, tool_retry

pytestmark = pytest.mark.live


# ============================================================================
# ListenBrainz — API contract
# ============================================================================

class TestListenBrainzContract:
    def test_listens_endpoint_shape(self, live_enc, test_user):
        status, body, headers = http_retry(live_enc, "GET", f"{live_enc.LB_API}/user/{test_user}/listens",
                                              params={"count": 3}, timeout=live_enc.SLOW_READ_TIMEOUT_S)
        assert status == 200, f"expected 200, got {status}: {body}"
        payload = body["payload"]
        assert payload["user_id"] == test_user
        assert isinstance(payload["listens"], list)
        assert len(payload["listens"]) <= 3, "the count parameter must limit the page size"
        for listen in payload["listens"]:
            assert isinstance(listen["listened_at"], int)
            meta = listen["track_metadata"]
            assert meta["artist_name"] and meta["track_name"]
            mapping = meta.get("mbid_mapping")
            if mapping and mapping.get("recording_mbid"):
                assert UUID_RE.match(mapping["recording_mbid"])

    def test_rate_limit_headers_are_present(self, live_enc, test_user):
        _, _, headers = http_retry(live_enc, "GET", f"{live_enc.LB_API}/user/{test_user}/listens",
                                      params={"count": 1}, timeout=live_enc.SLOW_READ_TIMEOUT_S)
        assert "X-RateLimit-Remaining" in headers and "X-RateLimit-Reset-In" in headers, \
            "http() relies on these headers to pace ListenBrainz requests"

    def test_unknown_user_is_404(self, live_enc):
        status, _, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/user/encore-no-such-user-9f3a/listens",
                                     params={"count": 1}, timeout=live_enc.SLOW_READ_TIMEOUT_S)
        assert status == 404


# ============================================================================
# ListenBrainz — get_recent_listens end to end
# ============================================================================

class TestGetRecentListensLive:
    def test_returns_trimmed_listens_newest_first(self, live_enc):
        result = tool_retry(live_enc.get_recent_listens, 5)
        assert result["success"], result["error"]
        listens = result["data"]["listens"]
        assert 1 <= len(listens) <= 5, "the test user should have listening history"
        for listen in listens:
            assert set(listen) == {"listened_at", "artist", "track", "recording_mbid"}
            assert listen["recording_mbid"] is None or UUID_RE.match(listen["recording_mbid"])
        times = [l["listened_at"] for l in listens]
        assert times == sorted(times, reverse=True), "listens should arrive newest first"

    def test_count_of_one(self, live_enc):
        result = tool_retry(live_enc.get_recent_listens, 1)
        assert result["success"] and len(result["data"]["listens"]) == 1

    def test_unknown_user_is_a_resource_error(self, live_enc, monkeypatch):
        monkeypatch.setattr(live_enc, "LB_USER", "encore-no-such-user-9f3a")
        result = tool_retry(live_enc.get_recent_listens, 1)
        assert result["error_category"] == "resource"


# ============================================================================
# MusicBrainz — API contract and search_recordings end to end
# ============================================================================

class TestMusicBrainzLive:
    def test_search_endpoint_shape(self, live_enc):
        status, body, _ = http_retry(live_enc, "GET", f"{live_enc.MB_API}/recording",
                                        params={"query": 'recording:"Black" AND artist:"Pearl Jam"', "limit": 3})
        assert status == 200, f"expected 200, got {status}"
        assert body["recordings"], "a well-known recording should be found"
        rec = body["recordings"][0]
        assert UUID_RE.match(rec["id"]) and rec["title"]
        assert all("name" in credit for credit in rec["artist-credit"])

    def test_search_recordings_finds_a_known_song(self, live_enc):
        result = tool_retry(live_enc.search_recordings, "Black", "Pearl Jam", limit=5)
        assert result["success"], result["error"]
        hits = result["data"]["recordings"]
        assert any(h["title"].lower() == "black" and "pearl jam" in h["artist"].lower() for h in hits)
        for h in hits:
            assert UUID_RE.match(h["recording_mbid"])
            assert isinstance(h["length_s"], int) and h["length_s"] >= 0

    def test_nonsense_search_is_a_resource_error(self, live_enc):
        result = tool_retry(live_enc.search_recordings, "zqxv encore nonexistent 7f3a9", "no such artist 7f3a9")
        assert result["error_category"] == "resource"

    def test_search_results_are_real_mbids(self, live_enc):
        """A found MBID must resolve by lookup: the basis for verify_recordings."""
        hit = tool_retry(live_enc.search_recordings, "Black", "Pearl Jam", limit=1)["data"]["recordings"][0]
        status, body, _ = http_retry(live_enc, "GET", f"{live_enc.MB_API}/recording/{hit['recording_mbid']}")
        assert status == 200 and body["id"] == hit["recording_mbid"] and body["title"] == hit["title"]

    def test_invented_mbid_does_not_resolve(self, live_enc):
        status, _, _ = http_retry(live_enc, "GET", f"{live_enc.MB_API}/recording/00000000-0000-4000-8000-000000000000")
        assert status == 404, "an invented MBID must not look real"


# ============================================================================
# Across both services
# ============================================================================

class TestCrossService:
    def test_listen_mbids_resolve_in_musicbrainz(self, live_enc):
        listens = tool_retry(live_enc.get_recent_listens, 25)["data"]["listens"]
        mapped = [l for l in listens if l["recording_mbid"]]
        if not mapped:
            pytest.skip("none of the recent listens are mapped to MusicBrainz recordings")
        status, body, _ = http_retry(live_enc, "GET", f"{live_enc.MB_API}/recording/{mapped[0]['recording_mbid']}")
        assert status == 200 and body["id"] == mapped[0]["recording_mbid"]

    def test_musicbrainz_pacing_is_real(self, live_enc):
        """Three searches in a row must be spaced out, and MusicBrainz must not refuse them (HTTP 503).

        MusicBrainz also returns 503 when its servers are busy, so a refusal gets one second
        chance after a pause. Two refused rounds in a row means the pacing isn't enough here.
        """
        def three_searches():
            start = time.monotonic()
            statuses = [live_enc.http("GET", f"{live_enc.MB_API}/recording",
                                      params={"query": f'recording:"{t}" AND artist:"Pearl Jam"', "limit": 1})[0]
                        for t in ("Alive", "Jeremy", "Even Flow")]
            return statuses, time.monotonic() - start

        statuses, elapsed = three_searches()
        assert elapsed >= 2 * live_enc.MB_MIN_INTERVAL_S - 0.05, "requests were not spaced out"
        if 503 in statuses:
            time.sleep(10)
            first = statuses
            statuses, _ = three_searches()
            assert 503 not in statuses, (
                f"MusicBrainz refused requests twice (statuses {first}, then {statuses}). The pacing isn't "
                "enough from this network; if you're on a shared network (e.g., campus), try from home")
        if 0 in statuses:
            pytest.skip(f"a search timed out (statuses {statuses}); MusicBrainz is slow right now, "
                        "which says nothing about pacing")
        assert statuses == [200, 200, 200], f"unexpected statuses {statuses}"


# ============================================================================
# Your own setup (runs only when you've configured a token)
# ============================================================================

def test_my_token_is_valid(live_enc):
    if not os.getenv("LISTENBRAINZ_TOKEN"):
        pytest.skip("LISTENBRAINZ_TOKEN is not set")
    status, body, _ = http_retry(live_enc, "GET", f"{live_enc.LB_API}/validate-token", auth=True)
    assert status == 200 and body.get("valid") is True, "ListenBrainz rejected your token"
    expected = os.getenv("LISTENBRAINZ_USER")
    if expected:
        assert body.get("user_name") == expected, "the token belongs to a different ListenBrainz user"
    assert os.environ["LISTENBRAINZ_TOKEN"] not in str(body)


if __name__ == "__main__":
    # Lets you run this file directly (e.g., VS Code's Run button): it hands off to pytest.
    raise SystemExit(pytest.main([__file__, "-v"]))
