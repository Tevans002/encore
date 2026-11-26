"""create_playlist must not create a playlist when the title lookup itself failed.

Before this change a failed lookup (429, 5xx, timeout) looked like "no such playlist",
so a retry or a resumed run could create a second playlist with the same title.
Uses the fixtures from test_provided_tools.py: enc, net, FakeResp.
"""
import json

import pytest
import requests

GOOD = "0b3f1c2e-1111-2222-3333-444455556666"


def _res(s):
    return json.loads(s)


def _existing(title):
    return {"playlists": [{"playlist": {
        "title": f"[Encore] {title}",
        "identifier": "https://listenbrainz.org/playlist/aaaa1111-0000-0000-0000-000000000000"}}]}


class TestFailClosed:
    @pytest.mark.parametrize("status,category", [(429, "transient"), (503, "transient"), (401, "fatal")])
    def test_failed_lookup_creates_nothing(self, enc, net, FakeResp, status, category):
        net.queue(FakeResp(status, {"error": "nope"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] is False and r["error_category"] == category
        assert "playlist lookup" in r["error"] and str(status) in r["error"]
        assert len(net.calls) == 1, "only the lookup may run; no create and no add"

    def test_network_failure_on_lookup_is_transient_and_creates_nothing(self, enc, net):
        net.queue(requests.Timeout("read timed out"))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] is False and r["error_category"] == "transient"
        assert len(net.calls) == 1

    def test_the_next_attempt_proceeds_once_the_lookup_works(self, enc, net, FakeResp):
        net.queue(FakeResp(503, {}))
        assert _res(enc.create_playlist("Rainy", [GOOD]))["success"] is False
        net.queue(FakeResp(200, {"playlists": []}))
        net.queue(FakeResp(200, {"status": "ok", "playlist_mbid": "pl-1"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] and r["data"]["playlist_mbid"] == "pl-1"


class TestUnchangedBehaviour:
    def test_no_match_still_creates(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"playlists": []}))
        net.queue(FakeResp(200, {"status": "ok", "playlist_mbid": "pl-1"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] and len(net.calls) == 2

    def test_other_titles_do_not_match(self, enc, net, FakeResp):
        net.queue(FakeResp(200, _existing("Sunny")))
        net.queue(FakeResp(200, {"status": "ok", "playlist_mbid": "pl-2"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] and r["data"]["playlist_mbid"] == "pl-2"

    def test_a_match_still_adds_to_the_existing_playlist(self, enc, net, FakeResp):
        net.queue(FakeResp(200, _existing("Rainy")))
        net.queue(FakeResp(200, {"status": "ok"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] and "not duplicated" in r["data"]["note"]
        assert len(net.calls) == 2
