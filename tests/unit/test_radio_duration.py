"""lb_radio passes each track's duration (ms) through so code can select by length."""
import json

MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL = "https://musicbrainz.org/recording/" + MBID


def _body(tracks):
    return {"payload": {"jspf": {"playlist": {"track": tracks}}, "feedback": []}}


class TestRadioDuration:
    def test_duration_is_passed_through_in_ms(self, enc, net, FakeResp):
        net.queue(FakeResp(200, _body([{"identifier": [URL], "title": "t", "creator": "c",
                                        "duration": 215000}])))
        r = json.loads(enc.lb_radio("tag:(ambient)"))
        t = r["data"]["tracks"][0]
        assert r["success"] is True and t["duration"] == 215000 and t["recording_mbid"] == MBID

    def test_missing_duration_is_none(self, enc, net, FakeResp):
        net.queue(FakeResp(200, _body([{"identifier": URL, "title": "t", "creator": "c"}])))
        r = json.loads(enc.lb_radio("tag:(ambient)"))
        assert r["data"]["tracks"][0]["duration"] is None
