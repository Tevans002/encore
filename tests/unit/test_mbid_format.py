"""Local MBID format checks on the ListenBrainz write tools, plus server failure reasons.

A format check says nothing about whether an MBID exists; that is verify_recordings.
Uses the fixtures from test_provided_tools.py: enc, net, FakeResp.
"""
import json

import pytest

GOOD = "0b3f1c2e-1111-2222-3333-444455556666"
BAD_VALUES = ["", "abc", "g" * 8 + GOOD[8:], GOOD + " ", GOOD[:-1],
              GOOD.replace("-", "_"), "x" * 36, 12345, None, ["a"]]


def _res(s):
    return json.loads(s)


class TestHelper:
    def test_good_and_uppercase_pass(self, enc):
        assert enc._mbid_problem(GOOD, "m") is None
        assert enc._mbid_problem(GOOD.upper(), "m") is None

    @pytest.mark.parametrize("value", BAD_VALUES)
    def test_bad_values_are_named(self, enc, value):
        msg = enc._mbid_problem(value, "recording_mbid")
        assert msg and msg.startswith("recording_mbid")

    def test_long_value_is_cut(self, enc):
        assert len(enc._mbid_problem("x" * 5000, "m")) < 400

    def test_list_reports_the_index(self, enc):
        msg = enc._mbid_list_problem([GOOD, GOOD, "bad"], "recording_mbids")
        assert msg.startswith("recording_mbids[2]")

    def test_list_must_be_a_list(self, enc):
        assert "must be a list" in enc._mbid_list_problem(GOOD, "recording_mbids")

    def test_good_list_passes(self, enc):
        assert enc._mbid_list_problem([GOOD, GOOD.upper()], "recording_mbids") is None


class TestSetFeedback:
    @pytest.mark.parametrize("value", ["abc", "g" * 36, GOOD[:-1]])
    def test_bad_mbid_is_rejected_before_any_call(self, enc, net, value):
        r = _res(enc.set_feedback(value, 1))
        assert r["success"] is False and r["error_category"] == "input"
        assert "recording_mbid" in r["error"] and net.calls == []

    def test_empty_mbid_keeps_its_existing_message(self, enc, net):
        assert "must not be empty" in _res(enc.set_feedback("", 1))["error"]

    def test_good_mbid_goes_through(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"status": "ok"}))
        r = _res(enc.set_feedback(GOOD, 1))
        assert r["success"] and r["data"]["recording_mbid"] == GOOD

    def test_server_reason_is_passed_on(self, enc, net, FakeResp):
        net.queue(FakeResp(400, {"error": "recording not found"}))
        r = _res(enc.set_feedback(GOOD, 1))
        assert "400" in r["error"] and "recording not found" in r["error"]


class TestCreatePlaylist:
    def test_bad_mbid_is_rejected_before_any_call(self, enc, net):
        r = _res(enc.create_playlist("Rainy", [GOOD, GOOD, "bad"]))
        assert r["success"] is False and r["error_category"] == "input"
        assert "recording_mbids[2]" in r["error"] and net.calls == []

    def test_good_list_creates(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"playlists": []}))
        net.queue(FakeResp(200, {"status": "ok", "playlist_mbid": "pl-1"}))
        r = _res(enc.create_playlist("Rainy", [GOOD]))
        assert r["success"] and r["data"]["track_count"] == 1


class TestAddPlaylistItems:
    def test_bad_mbid_is_rejected_before_any_call(self, enc, net):
        r = _res(enc.add_playlist_items("pl-1", [GOOD, "nope"]))
        assert r["success"] is False and "recording_mbids[1]" in r["error"] and net.calls == []

    def test_good_list_adds(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"status": "ok"}))
        r = _res(enc.add_playlist_items("pl-1", [GOOD]))
        assert r["success"] and r["data"]["added_count"] == 1

    def test_server_reason_is_passed_on(self, enc, net, FakeResp):
        net.queue(FakeResp(400, {"error": "track unknown"}))
        r = _res(enc.add_playlist_items("pl-1", [GOOD]))
        assert "400" in r["error"] and "track unknown" in r["error"]


class TestDeletePlaylist:
    def test_server_reason_on_a_failed_delete(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"playlist": {"title": enc.ENCORE_TITLE_PREFIX + " x"}}))
        net.queue(FakeResp(500, {"error": "database busy"}))
        r = _res(enc.delete_playlist("pl-1"))
        assert r["error_category"] == "transient"
        assert "500" in r["error"] and "database busy" in r["error"]

    def test_non_encore_playlist_is_still_refused(self, enc, net, FakeResp):
        net.queue(FakeResp(200, {"playlist": {"title": "My own playlist"}}))
        r = _res(enc.delete_playlist("pl-1"))
        assert r["success"] is False and "refusing to delete" in r["error"]
