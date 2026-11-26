"""ListenBrainz failure reasons reach the model (and the trace), safely.

Uses the fixtures from test_provided_tools.py: enc, net, FakeResp.
"""
import json

import pytest

GENRE_ERR = ("LB Radio generation failed: cannot parse prompt: 'Unrecognized element name "
             "'genre:'. Must be one of: artist,tag,collection,playlist,stats,recs,country'")


def _res(s):
    return json.loads(s)


class TestRadioFailureReason:
    def test_server_reason_and_status_are_in_the_message(self, enc, net, FakeResp):
        net.queue(FakeResp(400, {"code": 400, "error": GENRE_ERR}))
        r = _res(enc.lb_radio("genre:metal"))
        assert r["success"] is False and r["error_category"] == "input"
        assert "400" in r["error"]
        assert "Must be one of: artist,tag" in r["error"]

    def test_transient_status_keeps_its_category(self, enc, net, FakeResp):
        net.queue(FakeResp(503, {"error": "busy"}))
        r = _res(enc.lb_radio("tag:(metal)"))
        assert r["error_category"] == "transient" and "503" in r["error"]

    def test_long_reason_is_one_line_and_capped(self, enc, net, FakeResp):
        net.queue(FakeResp(400, {"error": "x\n" * 5000}))
        r = _res(enc.lb_radio("tag:(metal)"))
        assert "\n" not in r["error"]
        assert len(r["error"]) <= enc.SERVER_REASON_CHARS + 100

    def test_token_is_scrubbed(self, enc, net, FakeResp, monkeypatch):
        monkeypatch.setattr(enc, "LB_TOKEN", "SECRET-TOKEN-XYZ")
        net.queue(FakeResp(400, {"error": "bad header Token SECRET-TOKEN-XYZ"}))
        r = _res(enc.lb_radio("tag:(metal)"))
        assert "SECRET-TOKEN-XYZ" not in json.dumps(r)

    def test_success_is_unchanged(self, enc, net, FakeResp):
        track = {"identifier": ["https://musicbrainz.org/recording/abc"], "title": "t", "creator": "c"}
        net.queue(FakeResp(200, {"payload": {"jspf": {"playlist": {"track": [track]}}, "feedback": []}}))
        r = _res(enc.lb_radio("tag:(metal)"))
        assert r["success"] and r["data"]["tracks"][0]["recording_mbid"] == "abc"


class TestCreatePlaylistFailureReason:
    def test_reason_and_status_are_in_the_message(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"playlists": []}))   # title lookup: nothing exists yet
        net.queue(FakeResp(400, {"error": "bad track identifier"}))
        r = _res(enc.create_playlist("Rainy day", ["00000000-0000-0000-0000-000000000000"]))
        assert r["success"] is False and "400" in r["error"]
        assert "bad track identifier" in r["error"]


class TestHelper:
    @pytest.mark.parametrize("body", [None, "", 123, ["a"], {}])
    def test_odd_bodies_give_the_plain_message(self, enc, body):
        r = enc._http_fail("Thing", 502, body)
        assert r.error == "Thing failed (HTTP 502)." and r.error_category == "transient"

    def test_status_zero_ignores_the_exception_name(self, enc):
        r = enc._http_fail("Thing", 0, "ConnectionError")
        assert r.error == "Thing failed (HTTP 0)." and r.error_category == "transient"

    def test_text_body_is_used_when_there_was_a_response(self, enc):
        r = enc._http_fail("Thing", 400, "plain text reason")
        assert r.error.endswith("Server said: plain text reason")
