"""
Tests for the tools and helpers provided in the Encore starter.

    python -m pytest tests/unit      # this layer only: offline, fake network and clock

These tests must keep passing as you build Encore. If you change a provided
function on purpose, update its tests and say why in your README.
"""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, for helpers.py
from helpers import parse

LISTEN_PAGE = {
    "payload": {
        "count": 2,
        "listens": [
            {"listened_at": 1759200000,
             "track_metadata": {"artist_name": "Khruangbin", "track_name": "Maria También",
                                "release_name": "Con Todo El Mundo",
                                "mbid_mapping": {"recording_mbid": "0a1b2c3d-0000-4000-8000-000000000001"}}},
            {"listened_at": 1759199000,
             "track_metadata": {"artist_name": "Unmapped Artist", "track_name": "Local File Song"}},
        ],
    }
}

RECORDING_SEARCH = {
    "recordings": [
        {"id": "5b11f4ce-0000-4000-8000-000000000002", "title": "Black", "length": 343000,
         "first-release-date": "1991-08-27",
         "artist-credit": [{"name": "Pearl Jam"}]},
        {"id": "5b11f4ce-0000-4000-8000-000000000003", "title": "Under Pressure", "length": None,
         "artist-credit": [{"name": "Queen", "joinphrase": " & "}, {"name": "David Bowie"}]},
    ]
}


# ============================================================================
# ToolResult helpers
# ============================================================================

class TestToolResult:
    def test_ok_and_fail_follow_the_contract(self, enc):
        assert parse(enc.ok({"x": 1}).to_json()) == {"success": True, "data": {"x": 1},
                                                     "error": None, "error_category": None}
        bad = parse(enc.fail("input", "count must be positive").to_json())
        assert bad["error_category"] == "input" and "count" in bad["error"]

    @pytest.mark.parametrize("status,category", [
        (0, "transient"), (429, "transient"), (500, "transient"), (503, "transient"),
        (401, "fatal"), (403, "fatal"),
        (404, "resource"),
        (400, "input"), (422, "input"),
    ])
    def test_classify_status(self, enc, status, category):
        assert enc.classify_status(status) == category


# ============================================================================
# http(): politeness, auth, rate limits, failures
# ============================================================================

class TestHttp:
    def test_every_request_identifies_the_app(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {}), FakeResp(200, {}))
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        enc.http("GET", f"{enc.MB_API}/recording", params={"query": "x"})
        for call in net.calls:
            ua = call["headers"]["User-Agent"]
            assert ua.startswith("Encore") and "@" in ua, "User-Agent must name the app and a contact"

    def test_token_only_sent_when_asked(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {}), FakeResp(200, {}))
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        enc.http("POST", f"{enc.LB_API}/playlist/create", body={}, auth=True)
        assert "Authorization" not in net.calls[0]["headers"]
        assert net.calls[1]["headers"]["Authorization"] == "Token SECRET-TOKEN-123"

    def test_musicbrainz_requests_ask_for_json(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {}))
        enc.http("GET", f"{enc.MB_API}/artist/abc", params={"inc": "url-rels"})
        assert net.calls[0]["params"] == {"inc": "url-rels", "fmt": "json"}

    def test_musicbrainz_requests_are_spaced_out(self, enc, net, clock, FakeResp):
        net.queue(FakeResp(200, {}), FakeResp(200, {}), FakeResp(200, {}))
        for _ in range(3):
            enc.http("GET", f"{enc.MB_API}/recording", params={"query": "x"})
        assert len(clock.sleeps) == 2, "the 2nd and 3rd MusicBrainz calls must wait"
        assert all(s >= 1.0 for s in clock.sleeps)

    def test_listenbrainz_waits_when_rate_limit_exhausted(self, enc, net, clock, FakeResp):
        net.queue(FakeResp(200, {}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "7"}),
                  FakeResp(200, {}))
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        assert clock.sleeps == [7.0]

    def test_listenbrainz_does_not_wait_with_quota_left(self, enc, net, clock, FakeResp):
        net.queue(FakeResp(200, {}, headers={"X-RateLimit-Remaining": "12", "X-RateLimit-Reset-In": "7"}),
                  FakeResp(200, {}))
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        assert clock.sleeps == []

    def test_network_error_is_status_zero(self, enc, net):
        net.queue(requests.ConnectionError("no route to host"))
        status, body, headers = enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        assert status == 0 and "ConnectionError" in body and headers == {}

    def test_timeout_is_passed_to_requests(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {}), FakeResp(200, {}))
        enc.http("GET", f"{enc.LB_API}/user/test_user/listens")
        enc.http("POST", f"{enc.LB_API}/playlist/create", body={}, auth=True, timeout=enc.WRITE_TIMEOUT_S)
        assert net.calls[0]["timeout"] == enc.READ_TIMEOUT_S == 5
        assert net.calls[1]["timeout"] == enc.WRITE_TIMEOUT_S == 15

    def test_non_json_body_returned_as_text(self, enc, net, FakeResp):
        net.queue(FakeResp(503, None, text="<html>Service Unavailable</html>"))
        status, body, _ = enc.http("GET", f"{enc.MB_API}/recording", params={"query": "x"})
        assert status == 503 and "Service Unavailable" in body


# ============================================================================
# get_recent_listens
# ============================================================================

class TestGetRecentListens:
    def test_happy_path_trims_to_useful_fields(self, enc, net, FakeResp):
        net.queue(FakeResp(200, LISTEN_PAGE))
        result = parse(enc.get_recent_listens(2))
        assert result["success"]
        first, second = result["data"]["listens"]
        assert first == {"listened_at": 1759200000, "artist": "Khruangbin", "track": "Maria También",
                         "recording_mbid": "0a1b2c3d-0000-4000-8000-000000000001"}
        assert second["recording_mbid"] is None, "listens without an MBID mapping must not crash"

    def test_calls_the_right_endpoint_for_the_configured_user(self, enc, net, FakeResp):
        net.queue(FakeResp(200, LISTEN_PAGE))
        enc.get_recent_listens(10)
        call = net.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == "https://api.listenbrainz.org/1/user/test_user/listens"
        assert call["params"] == {"count": 10}
        assert call["timeout"] == enc.SLOW_READ_TIMEOUT_S, "listens can be slow; use the slow-read override"
        assert "Authorization" not in call["headers"], "public listens don't need the token"

    @pytest.mark.parametrize("count", [0, -5, 101, 1000])
    def test_rejects_out_of_range_count_without_calling_the_api(self, enc, net, count):
        result = parse(enc.get_recent_listens(count))
        assert result["error_category"] == "input" and str(count) in result["error"]
        assert net.calls == []

    @pytest.mark.parametrize("count", [1, 100])
    def test_accepts_boundary_counts(self, enc, net, FakeResp, count):
        net.queue(FakeResp(200, {"payload": {"listens": []}}))
        assert parse(enc.get_recent_listens(count))["success"]

    @pytest.mark.parametrize("status,category", [
        (429, "transient"), (503, "transient"), (401, "fatal"), (404, "resource"), (400, "input"),
    ])
    def test_http_failures_become_categorized_errors(self, enc, net, FakeResp, status, category):
        net.queue(FakeResp(status, {"error": "nope"}))
        result = parse(enc.get_recent_listens(5))
        assert result["error_category"] == category and str(status) in result["error"]

    def test_network_failure_is_transient(self, enc, net):
        net.queue(requests.Timeout("read timed out"))
        assert parse(enc.get_recent_listens(5))["error_category"] == "transient"

    def test_token_never_appears_in_output(self, enc, net, FakeResp):
        net.queue(FakeResp(200, LISTEN_PAGE), FakeResp(401, {"error": "bad token"}))
        assert "SECRET-TOKEN-123" not in enc.get_recent_listens(5)
        assert "SECRET-TOKEN-123" not in enc.get_recent_listens(5)


# ============================================================================
# search_recordings
# ============================================================================

class TestSearchRecordings:
    def test_happy_path_trims_and_converts(self, enc, net, FakeResp):
        net.queue(FakeResp(200, RECORDING_SEARCH))
        result = parse(enc.search_recordings("Black", "Pearl Jam"))
        black, pressure = result["data"]["recordings"]
        assert black == {"recording_mbid": "5b11f4ce-0000-4000-8000-000000000002", "title": "Black",
                         "artist": "Pearl Jam", "length_s": 343, "first_release": "1991-08-27"}
        assert pressure["artist"] == "Queen & David Bowie", "join phrases must be kept"
        assert pressure["length_s"] == 0 and pressure["first_release"] is None

    def test_builds_a_fielded_query(self, enc, net, FakeResp):
        net.queue(FakeResp(200, RECORDING_SEARCH), FakeResp(200, RECORDING_SEARCH))
        enc.search_recordings("Black", "Pearl Jam", limit=3)
        enc.search_recordings("Black")
        with_artist, without = net.calls[0]["params"], net.calls[1]["params"]
        assert net.calls[0]["url"] == "https://musicbrainz.org/ws/2/recording"
        assert with_artist == {"query": 'recording:"Black" AND artist:"Pearl Jam"', "limit": 3, "fmt": "json"}
        assert without["query"] == 'recording:"Black"' and without["limit"] == 5

    def test_no_matches_is_a_resource_error_not_an_empty_list(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"recordings": []}))
        result = parse(enc.search_recordings("Song That Does Not Exist", "Nobody"))
        assert result["error_category"] == "resource"
        assert "Song That Does Not Exist" in result["error"] and "Nobody" in result["error"]

    @pytest.mark.parametrize("title", ["", "   "])
    def test_rejects_empty_title_without_calling_the_api(self, enc, net, title):
        assert parse(enc.search_recordings(title))["error_category"] == "input"
        assert net.calls == []

    @pytest.mark.parametrize("status,category", [(503, "transient"), (400, "input"), (403, "fatal")])
    def test_http_failures_become_categorized_errors(self, enc, net, FakeResp, status, category):
        net.queue(FakeResp(status, None, text="error page"))
        assert parse(enc.search_recordings("Black"))["error_category"] == category

    def test_respects_musicbrainz_rate_limit_across_tools(self, enc, net, clock, FakeResp):
        net.queue(FakeResp(200, RECORDING_SEARCH), FakeResp(200, RECORDING_SEARCH))
        enc.search_recordings("Black")
        enc.search_recordings("Alive")
        assert len(clock.sleeps) == 1 and clock.sleeps[0] >= 1.0


# ============================================================================
# Registry and retry helper
# ============================================================================

class TestRegistryAndBackoff:
    def test_provided_tools_are_registered(self, enc):
        assert enc.TOOLS["get_recent_listens"] is enc.get_recent_listens
        assert enc.TOOLS["search_recordings"] is enc.search_recordings

    def test_every_registered_tool_is_callable(self, enc):
        assert all(callable(fn) for fn in enc.TOOLS.values())

    def test_backoff_grows_and_is_capped(self, enc, monkeypatch):
        monkeypatch.setattr(enc.random, "uniform", lambda lo, hi: hi)   # take the ceiling
        assert enc.backoff_delays(attempts=7, base=1.0, cap=30.0) == [1, 2, 4, 8, 16, 30, 30]

    def test_backoff_is_jittered(self, enc):
        runs = {tuple(enc.backoff_delays()) for _ in range(20)}
        assert len(runs) > 1, "delays should vary between runs (jitter)"
        assert all(0 <= d <= 30 for run in runs for d in run)

    def test_loop_limits_match_the_spec(self, enc):
        assert enc.MAX_STEPS == 12
        assert enc.MB_MIN_INTERVAL_S >= 1.0


if __name__ == "__main__":
    # Lets you run this file directly (e.g., VS Code's Run button): it hands off to pytest.
    raise SystemExit(pytest.main([__file__, "-v"]))
