"""Checkpoints: keyed by run_id and stage, never replayed, resume plan, clear-all, deny strings.

Includes a kill-and-resume run of the step machine (states.py) against the recorded fixtures with fake
tools. No network, no model.
"""
import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import checkpoints as ck
import states as st

ORDER = st.STATES
T = "2026-11-27T14:15:00"
FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = int(datetime.datetime(2026, 11, 24, 14, 15, tzinfo=ZoneInfo("America/Denver")).timestamp())
CFG = st.Config(table_tags=("ambient", "piano", "jazz", "folk"), fallback_zone="America/Denver",
                default_prompt="ambient")


@pytest.fixture
def store(tmp_path):
    s = ck.Store(str(tmp_path / "ck.sqlite"), deny=["tok123abc", "zzuser"])
    yield s
    s.close()


class TestSaveLoad:
    def test_round_trip(self, store):
        store.save("r1", "WEATHER", "done", {"forecast": [1, 2]}, T)
        assert store.load("r1", "WEATHER") == {"status": "done", "payload": {"forecast": [1, 2]}, "saved_at": T}
        assert store.load("r1", "CONTEXT") is None

    def test_bad_status_and_blank_ids_rejected(self, store):
        with pytest.raises(ValueError):
            store.save("r1", "WEATHER", "finished", {}, T)
        with pytest.raises(ValueError):
            store.save("", "WEATHER", "done", {}, T)

    def test_started_then_failed_then_done_is_allowed(self, store):
        for status in ("started", "failed", "started", "done"):
            store.save("r1", "WRITE", status, {"n": status}, T)
        assert store.load("r1", "WRITE")["status"] == "done"

    def test_a_done_stage_is_never_saved_again(self, store):
        store.save("r1", "CURATE", "done", {"v": 1}, T)
        for status in ("started", "failed", "done"):
            with pytest.raises(ck.AlreadyDone):
                store.save("r1", "CURATE", status, {"v": 2}, T)
        assert store.load("r1", "CURATE")["payload"] == {"v": 1}

    def test_runs_are_separate(self, store):
        store.save("r1", "CURATE", "done", {"a": 1}, T)
        store.save("r2", "CURATE", "done", {"a": 2}, T)
        assert store.load("r1", "CURATE")["payload"] == {"a": 1} and store.runs() == ["r1", "r2"]

    def test_survives_reopening_the_file(self, tmp_path):
        p = str(tmp_path / "ck.sqlite")
        a = ck.Store(p)
        a.save("r1", "WEATHER", "done", {"x": 1}, T)
        a.close()
        b = ck.Store(p)
        assert b.load("r1", "WEATHER")["payload"] == {"x": 1}
        b.close()


class TestRejections:
    @pytest.mark.parametrize("payload", [{"t": "key tok123abc here"}, {"u": "made for ZZUser"}])
    def test_deny_strings_are_refused_and_nothing_is_stored(self, store, payload):
        with pytest.raises(ck.Rejected):
            store.save("r1", "WEATHER", "done", payload, T)
        assert store.count() == 0

    def test_not_json_refused(self, store):
        with pytest.raises(ck.Rejected):
            store.save("r1", "WEATHER", "done", {"x": {1, 2}}, T)
        assert store.count() == 0

    def test_too_large_refused(self, store):
        with pytest.raises(ck.Rejected):
            store.save("r1", "WEATHER", "done", {"x": "a" * (ck.MAX_PAYLOAD_BYTES + 1)}, T)

    def test_short_deny_strings_are_ignored(self, tmp_path):
        s = ck.Store(str(tmp_path / "x.sqlite"), deny=["ab"])
        s.save("r1", "WEATHER", "done", {"x": "ab ab"}, T)
        s.close()


class TestResumePlan:
    def test_unknown_run(self, store):
        with pytest.raises(ck.UnknownRun):
            store.resume_plan("nope", ORDER)

    def test_restarts_from_the_first_stage_that_is_not_done(self, store):
        for stg in ("LOCATION", "WEATHER", "CONTEXT"):
            store.save("r1", stg, "done", {}, T)
        store.save("r1", "CURATE", "failed", {}, T)
        plan = store.resume_plan("r1", ORDER)
        assert plan["first"] == "CURATE" and plan["skip"] == ["LOCATION", "WEATHER", "CONTEXT"]
        assert plan["run"] == ["CURATE", "VERIFY", "APPROVE", "WRITE"] and plan["check_duplicates"] is False

    def test_a_stage_left_started_asks_for_a_duplicate_check(self, store):
        for stg in ORDER[:-1]:
            store.save("r1", stg, "done", {}, T)
        store.save("r1", "WRITE", "started", {}, T)
        plan = store.resume_plan("r1", ORDER)
        assert plan["first"] == "WRITE" and plan["check_duplicates"] is True

    def test_all_done_means_nothing_to_run(self, store):
        for stg in ORDER:
            store.save("r1", stg, "done", {}, T)
        plan = store.resume_plan("r1", ORDER)
        assert plan["first"] is None and plan["run"] == [] and plan["skip"] == list(ORDER)

    def test_a_skipped_stage_with_later_stages_done_is_an_error(self, store):
        store.save("r1", "WEATHER", "failed", {}, T)
        store.save("r1", "CONTEXT", "done", {}, T)
        with pytest.raises(ck.CheckpointError):
            store.resume_plan("r1", ORDER)

    def test_unknown_stage_name_is_an_error(self, store):
        store.save("r1", "MYSTERY", "done", {}, T)
        with pytest.raises(ck.CheckpointError):
            store.resume_plan("r1", ORDER)

    def test_latest_done(self, store):
        store.save("r1", "LOCATION", "done", {"a": 1}, T)
        store.save("r1", "WEATHER", "done", {"a": 2}, T)
        store.save("r1", "CONTEXT", "failed", {"a": 3}, T)
        assert store.latest_done("r1") == {"stage": "WEATHER", "payload": {"a": 2}, "saved_at": T}
        store.save("r2", "LOCATION", "failed", {}, T)
        assert store.latest_done("r2") is None


class TestClear:
    def test_clear_run_only_touches_that_run(self, store):
        store.save("r1", "WEATHER", "done", {}, T)
        store.save("r2", "WEATHER", "done", {}, T)
        assert store.clear_run("r1") == 1 and store.runs() == ["r2"]

    def test_clear_all_empties_the_store_and_reports_the_count(self, store):
        for run_id in ("r1", "r2"):
            for stg in ("WEATHER", "CONTEXT"):
                store.save(run_id, stg, "done", {}, T)
        assert store.clear_all() == 4 and store.count() == 0 and store.runs() == []
        assert store.clear_all() == 0

    def test_cleared_data_is_gone_from_the_file(self, tmp_path):
        p = str(tmp_path / "ck.sqlite")
        s = ck.Store(p)
        s.save("r1", "CURATE", "done", {"marker": "UNIQUE-MARKER-123"}, T)
        s.clear_all()
        s.close()
        assert b"UNIQUE-MARKER-123" not in Path(p).read_bytes()


class TestRunId:
    def test_format_is_stable(self):
        assert ck.new_run_id(T, "a1b2") == "r-20261127T141500-a1b2"

    def test_suffix_must_be_alphanumeric(self):
        with pytest.raises(ValueError):
            ck.new_run_id(T, "a/../b")


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def pool():
    return [{"recording_mbid": st.track_mbid(t), "title": t["title"], "creator": t["creator"],
             "duration": t.get("duration")}
            for t in load("radio_50_tracks.json")["payload"]["jspf"]["playlist"]["track"]]


class Killed(Exception):
    pass


def run(store, run_id, d, kill_before=None, writes=None):
    """Stand-in driver: checkpoints each state when next_step leaves it. `kill_before` simulates a crash."""
    writes = writes if writes is not None else []
    if d.city and store.load(run_id, "LOCATION") is None:
        store.save(run_id, "LOCATION", "done", {"run_data": d.to_dict()}, T)
    for _ in range(40):
        s = st.next_step(d, CFG)
        if s.state in ("DONE", "STOP"):
            return s
        if s.action == kill_before:
            raise Killed(s.action)
        if s.action == "dispatch_weather":
            d.location = {"timezone": "America/Denver"}
            d.forecast = load("forecast_missoula.json")["structured_content"]["hourly"][12:18]
        elif s.action == "fetch_listens":
            st.apply_listens(d, st.excluded_window(load("listens_covered.json")["payload"]["listens"], NOW))
        elif s.action == "call_radio":
            st.record_radio_call(d, CFG, "ambient", pool())
        elif s.action == "select":
            st.run_select(d)
        elif s.action == "verify":
            st.record_verify(d, s.detail["mbids"], [])
        elif s.action == "request_approval":
            d.approved = True
        elif s.action == "write":
            store.save(run_id, "WRITE", "started", {"run_data": d.to_dict()}, T)
            writes.append(list(d.selection))
            d.playlist_mbid = "playlist-1"
        else:
            raise AssertionError(s)
        nxt = st.next_step(d, CFG)
        if nxt.state != s.state:                     # the state just finished
            store.save(run_id, s.state, "done", {"run_data": d.to_dict()}, T)
    raise AssertionError("no end")


class TestKillAndResume:
    def test_crash_before_verify_then_resume_skips_completed_stages(self, store):
        d = st.RunData(city="Missoula")
        with pytest.raises(Killed):
            run(store, "r1", d, kill_before="verify")
        assert store.statuses("r1") == {"LOCATION": "done", "WEATHER": "done", "CONTEXT": "done", "CURATE": "done"}
        plan = store.resume_plan("r1", ORDER)
        assert plan["first"] == "VERIFY" and plan["skip"] == ["LOCATION", "WEATHER", "CONTEXT", "CURATE"]
        d2 = st.RunData.from_dict(store.latest_done("r1")["payload"]["run_data"])
        assert st.next_step(d2, CFG).state == "VERIFY"
        run(store, "r1", d2)
        assert d2.playlist_mbid == "playlist-1" and len(d2.selection) == 10

    def test_resume_does_not_call_radio_again_once_curate_is_done(self, store):
        d = st.RunData(city="Missoula")
        with pytest.raises(Killed):
            run(store, "r1", d, kill_before="verify")
        before = d.radio_calls
        d2 = st.RunData.from_dict(store.latest_done("r1")["payload"]["run_data"])
        assert d2.radio_calls == before
        run(store, "r1", d2)
        assert d2.radio_calls == before == 1

    def test_one_write_only_when_resumed_after_a_finished_run(self, store):
        writes = []
        d = st.RunData(city="Missoula")
        run(store, "r1", d, writes=writes)
        assert len(writes) == 1
        assert store.resume_plan("r1", ORDER)["first"] is None
        with pytest.raises(ck.AlreadyDone):
            store.save("r1", "WRITE", "done", {}, T)

    def test_crash_during_write_is_flagged_for_a_duplicate_check(self, store):
        d = st.RunData(city="Missoula")
        for stg in ORDER[:-1]:
            store.save("r1", stg, "done", {"run_data": d.to_dict()}, T)
        store.save("r1", "WRITE", "started", {"run_data": d.to_dict()}, T)
        plan = store.resume_plan("r1", ORDER)
        assert plan["first"] == "WRITE" and plan["check_duplicates"] is True

    def test_forget_clears_every_checkpoint(self, store):
        d = st.RunData(city="Missoula")
        run(store, "r1", d)
        assert store.count() > 0
        assert store.clear_all() > 0 and store.count() == 0
        with pytest.raises(ck.UnknownRun):
            store.resume_plan("r1", ORDER)
