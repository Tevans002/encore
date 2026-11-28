"""Step machine: completion, skipping, escalation ladder, tool gate, statuses, and a Golden 1 slice.

No network, no model. The slice at the bottom runs the whole state order against the recorded fixtures
with fake tools (it stands in for the driver that does not exist yet).
"""
import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import states as st

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = int(datetime.datetime(2026, 11, 24, 14, 15, tzinfo=ZoneInfo("America/Denver")).timestamp())
CFG = st.Config(table_tags=("ambient", "piano", "jazz", "folk"), fallback_zone="America/Denver",
                default_prompt="ambient")


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def pool():
    """Radio fixture in the shape the lb_radio tool returns."""
    out = []
    for t in load("radio_50_tracks.json")["payload"]["jspf"]["playlist"]["track"]:
        out.append({"recording_mbid": st.track_mbid(t), "title": t["title"], "creator": t["creator"],
                    "duration": t.get("duration")})
    return out


def trk(i, secs=300, artist=None):
    return {"recording_mbid": f"m{i:03d}", "creator": artist or f"a{i}", "duration": secs * 1000}


def ready(**kw):
    """A run that is past LOCATION, WEATHER and CONTEXT."""
    d = st.RunData(city="Missoula", forecast=[{"time": "x"}],
                   listens={"status": "covered", "excluded_mbids": [], "in_window": 0, "unmapped": 0})
    for k, v in kw.items():
        setattr(d, k, v)
    return d


class TestConfig:
    def test_default_prompt_must_be_in_the_table(self):
        with pytest.raises(ValueError):
            st.Config(table_tags=("ambient",), fallback_zone="America/Denver", default_prompt="metal")

    def test_requires_all_three_inputs(self):
        with pytest.raises(TypeError):
            st.Config(table_tags=("ambient",))

    def test_empty_or_repeated_table_rejected(self):
        for tags in ((), ("a", "a")):
            with pytest.raises(ValueError):
                st.Config(table_tags=tags, fallback_zone="America/Denver", default_prompt="a")


class TestOrderAndSkipping:
    def test_no_city_asks_once_then_stops(self):
        d = st.RunData()
        assert st.next_step(d, CFG) == st.Step("LOCATION", "ask_city")
        st.record_ask_city(d)
        s = st.next_step(d, CFG)
        assert s.state == "STOP" and s.action == "stuck" and st.final_status(d) == "stuck"

    def test_city_given_up_front_skips_location_without_a_question(self):
        d = st.RunData()
        st.set_city(d, " Missoula ")
        assert d.city == "Missoula" and not d.asked_city
        assert st.next_step(d, CFG).state == "WEATHER"

    def test_empty_city_rejected(self):
        with pytest.raises(ValueError):
            st.set_city(st.RunData(), "  ")

    def test_states_follow_the_logged_order(self):
        d = st.RunData(city="Missoula")
        assert st.next_step(d, CFG).state == "WEATHER"
        d.forecast = []
        assert st.next_step(d, CFG).state == "CONTEXT"
        d.listens = {"status": "covered", "excluded_mbids": []}
        assert st.next_step(d, CFG) == st.Step("CURATE", "call_radio", {"default_prompt": None})

    def test_next_step_does_not_mutate(self):
        d = ready(tracks=pool())
        before = json.dumps(d.to_dict(), sort_keys=True)
        st.next_step(d, CFG)
        assert json.dumps(d.to_dict(), sort_keys=True) == before

    def test_weather_failure_continues_with_the_default_prompt(self):
        d = st.RunData(city="Missoula")
        st.escalate(d, "WEATHER", "fatal", "ConnectError")
        assert d.weather_failed and d.forecast is None and d.stop is None
        d.listens = {"status": "covered", "excluded_mbids": []}
        assert st.next_step(d, CFG).detail == {"default_prompt": "ambient"}

    def test_context_failure_becomes_unknown(self):
        d = st.RunData(city="Missoula", forecast=[])
        st.escalate(d, "CONTEXT", "transient")
        assert d.listens["status"] == "unknown" and st.next_step(d, CFG).state == "CURATE"

    def test_required_state_failure_stops_with_a_reason(self):
        d = ready()
        st.escalate(d, "CURATE", "fatal", "x" * 500)
        s = st.next_step(d, CFG)
        assert s.state == "STOP" and len(d.stop["message"]) == 300 and st.final_status(d) == "stuck"

    def test_zone_falls_back_when_weather_gave_none(self):
        assert st.effective_zone(st.RunData(), CFG) == "America/Denver"
        d = st.RunData(location={"timezone": "America/Chicago"})
        assert st.effective_zone(d, CFG) == "America/Chicago"


class TestAttempts:
    def test_three_attempts_then_exhausted(self):
        d = st.RunData()
        assert [st.record_attempt(d, "WEATHER") for _ in range(3)] == [2, 1, 0]
        assert st.attempts_exhausted(d, "WEATHER") and not st.attempts_exhausted(d, "CURATE")
        assert st.record_attempt(d, "WEATHER") == 0


class TestListensWindow:
    def test_covered_file_finds_two_recent_radio_repeats(self):
        w = st.excluded_window(load("listens_covered.json")["payload"]["listens"], NOW)
        radio = {t["recording_mbid"] for t in pool()}
        assert w["status"] == "covered" and w["in_window"] == 72
        assert len(set(w["excluded_mbids"]) & radio) == 2 and w["unmapped"] == 1

    def test_short_span_file_is_unknown(self):
        w = st.excluded_window(load("listens_short_span.json")["payload"]["listens"], NOW)
        assert w["status"] == "unknown" and w["in_window"] == 100

    def test_fewer_than_requested_means_the_whole_history(self):
        ls = [{"listened_at": NOW - 60, "track_metadata": {"mbid_mapping": {"recording_mbid": "m1"}}}]
        assert st.excluded_window(ls, NOW)["status"] == "covered"
        assert st.excluded_window([], NOW)["status"] == "covered"

    def test_cutoff_is_inclusive(self):
        cutoff = NOW - 3 * 86400
        ls = [{"listened_at": cutoff, "track_metadata": {"mbid_mapping": {"recording_mbid": "in"}}},
              {"listened_at": cutoff - 1, "track_metadata": {"mbid_mapping": {"recording_mbid": "out"}}}]
        w = st.excluded_window(ls, NOW, requested=2)
        assert w["excluded_mbids"] == ["in"] and w["status"] == "covered"


class TestSelectAndLadder:
    def test_excluded_tracks_are_never_selected(self):
        d = ready(tracks=pool())
        recent = [t["recording_mbid"] for t in d.tracks[:3]]
        d.listens["excluded_mbids"] = recent
        st.run_select(d)
        assert d.select_reason == "" and not set(d.selection) & set(recent) and len(d.selection) == 10
        assert 2700 <= d.select_total_s <= 3300

    def test_failed_select_climbs_the_ladder_in_order(self):
        d = ready()
        short = [trk(i, 100) for i in range(12)]            # 12 x 100 s: no 10 reach 2700 s
        assert st.record_radio_call(d, CFG, "ambient", short) == "first"
        assert st.next_step(d, CFG).action == "select"
        st.run_select(d)
        assert d.select_reason == "no_set_in_window"
        s = st.next_step(d, CFG)
        assert s.action == "recall_same_tag" and s.detail["tag"] == "ambient"
        assert st.record_radio_call(d, CFG, "ambient", short) == "same"
        st.run_select(d)
        s = st.next_step(d, CFG)
        assert s.action == "different_tag" and s.detail["candidates"] == ["piano", "jazz", "folk"]
        assert st.record_radio_call(d, CFG, "piano", short) == "different"
        st.run_select(d)
        s = st.next_step(d, CFG)
        assert d.radio_calls == 3 and s.action == "ask_user" and s.detail["offer"] == "relax_window"

    def test_radio_cap_is_three_calls_in_total(self):
        d = ready()
        st.record_radio_call(d, CFG, "ambient")
        st.record_radio_call(d, CFG, "ambient")
        st.record_radio_call(d, CFG, "piano")
        with pytest.raises(st.RadioCapReached):
            st.record_radio_call(d, CFG, "jazz")

    @pytest.mark.parametrize("tag", ["metal", "genre:metal", ""])
    def test_tag_outside_the_table_is_rejected(self, tag):
        with pytest.raises(ValueError):
            st.record_radio_call(ready(), CFG, tag)

    def test_repeating_a_tag_out_of_ladder_order_is_rejected(self):
        d = ready()
        st.record_radio_call(d, CFG, "ambient")
        st.record_radio_call(d, CFG, "piano")
        with pytest.raises(ValueError):
            st.record_radio_call(d, CFG, "ambient")

    def test_failed_first_call_retries_then_asks_the_user(self):
        d = ready()
        st.record_radio_call(d, CFG, "ambient")                 # 400, no tracks
        assert st.next_step(d, CFG).action == "call_radio"
        st.record_radio_call(d, CFG, "piano")
        st.record_radio_call(d, CFG, "piano")
        assert d.radio_calls == 3
        s = st.next_step(d, CFG)
        assert s.action == "ask_user" and s.detail["reason"] == "no_tracks"

    def test_failed_recall_keeps_the_old_pool(self):
        d = ready(tracks=[trk(i, 100) for i in range(12)])
        st.run_select(d)
        st.record_radio_call(d, CFG, "ambient", d.tracks)
        st.record_radio_call(d, CFG, "ambient", None)           # same tag again, call failed
        assert len(d.tracks) == 12

    def test_relaxing_the_window_only_after_the_user_agrees(self):
        d = ready(tracks=[trk(i, 250) for i in range(12)])      # 10 x 250 s = 2500 s: short of 2700
        st.run_select(d)
        assert d.select_reason == "no_set_in_window" and d.window == [2700, 3300]
        st.accept_relaxed_window(d)
        assert d.window == [2400, 3600] and d.select_reason is None
        assert st.next_step(d, CFG).action == "select"
        st.run_select(d)
        assert d.select_reason == "" and d.select_total_s == 2500
        with pytest.raises(ValueError):
            st.accept_relaxed_window(d)

    def test_declining_the_wider_window_stops_as_stuck(self):
        d = ready(tracks=[trk(i, 100) for i in range(12)])
        st.decline_relaxed_window(d)
        assert st.next_step(d, CFG).state == "STOP" and st.final_status(d) == "stuck"

    def test_still_failing_after_relaxing_is_stuck_not_a_loop(self):
        d = ready(tracks=[trk(i, 100) for i in range(12)], radio_calls=3, same_tag_retried=True,
                  other_tag_tried=True, tags_used=["ambient", "ambient", "piano"])
        st.accept_relaxed_window(d)
        st.run_select(d)
        s = st.next_step(d, CFG)
        assert s.state == "STOP" and s.action == "stuck"


class TestVerify:
    def selected(self):
        d = ready(tracks=pool())
        st.run_select(d)
        return d

    def test_only_unverified_mbids_are_checked(self):
        d = self.selected()
        st.record_verify(d, d.selection[:4], [])
        s = st.next_step(d, CFG)
        assert s.action == "verify" and s.detail["mbids"] == d.selection[4:]

    def test_all_valid_moves_on_to_approval(self):
        d = self.selected()
        st.record_verify(d, d.selection, [])
        assert st.next_step(d, CFG).action == "request_approval"

    def test_invalid_mbid_is_blocked_and_reselected_without_a_radio_call(self):
        d = self.selected()
        bad = d.selection[0]
        st.record_verify(d, d.selection[1:], [bad])
        assert d.selection is None and d.verify_reselects == 1 and d.radio_calls == 0
        assert st.next_step(d, CFG).action == "select"
        st.run_select(d)
        assert bad not in d.selection and len(d.selection) == 10

    def test_third_failed_round_goes_to_the_ladder(self):
        d = self.selected()
        for _ in range(3):
            st.record_verify(d, [], [d.selection[0]])
            if d.select_reason is None:
                st.run_select(d)
        assert d.verify_reselects == 3 and d.select_reason == "verify_failed"
        d.tags_used, d.radio_calls = ["ambient"], 1
        assert st.next_step(d, CFG).action == "recall_same_tag"


class TestGate:
    def test_tool_in_the_states_list_is_allowed(self):
        assert st.gate_check(st.RunData(), "CURATE", "lb_radio") == "allow"

    def test_first_violation_is_rejected_second_halts(self):
        d = st.RunData()
        assert st.gate_check(d, "CURATE", "create_playlist") == "reject" and not d.halted
        assert st.gate_check(d, "CURATE", "delete_playlist") == "halt" and d.halted
        assert st.next_step(d, CFG).action == "policy_halt" and st.final_status(d) == "policy_halt"

    def test_create_playlist_is_not_offered_before_verification_completes(self):
        for state in st.STATES:
            if state != "WRITE":
                assert "create_playlist" not in st.offered_tools(state)

    def test_write_before_approval_is_a_violation_even_in_the_write_state(self):
        d = st.RunData()
        assert st.gate_check(d, "WRITE", "create_playlist") == "reject"
        d2 = st.RunData(approved=True)
        assert st.gate_check(d2, "WRITE", "create_playlist") == "allow"

    def test_a_declined_run_cannot_write(self):
        assert st.gate_check(st.RunData(approved=False), "WRITE", "create_playlist") == "reject"


class TestStatus:
    def test_running_has_no_status(self):
        assert st.final_status(ready()) is None

    @pytest.mark.parametrize("kw,want", [
        (dict(playlist_mbid="p"), "answered"),
        (dict(playlist_mbid="p", weather_failed=True), "answered_low_confidence"),
        (dict(playlist_mbid="p", listens={"status": "unknown"}), "answered_low_confidence"),
        (dict(approved=False), "answered"),
        (dict(stop={"state": "WRITE", "kind": "fatal"}), "stuck"),
        (dict(stop={"state": "CURATE", "kind": "budget_exceeded"}), "budget_exceeded"),
        (dict(halted=True), "policy_halt"),
    ])
    def test_mapping(self, kw, want):
        assert st.final_status(ready(**kw)) == want

    def test_every_status_is_one_of_the_five_midterm_values(self):
        import messages
        for s in ("answered", "answered_low_confidence", "stuck", "budget_exceeded", "policy_halt"):
            assert s in messages.FINAL_STATUSES


class TestSerialization:
    def test_round_trip_through_json(self):
        d = ready(tracks=pool()[:5], tags_used=["ambient"], radio_calls=1)
        st.run_select(d)
        again = st.RunData.from_dict(json.loads(json.dumps(d.to_dict())))
        assert again == d

    def test_unknown_field_rejected(self):
        with pytest.raises(ValueError):
            st.RunData.from_dict({"nope": 1})


def drive(d, cfg, listens_name="listens_covered.json", weather="ok", decline=False, bad_mbids=()):
    """A stand-in driver: performs each named action with fixtures and fake tools."""
    trace = []
    for _ in range(40):
        s = st.next_step(d, cfg)
        trace.append((s.state, s.action))
        if s.state in ("DONE", "STOP"):
            return trace
        if s.action == "ask_city":
            st.record_ask_city(d)
        elif s.action == "dispatch_weather":
            if weather == "ok":
                d.location = {"latitude": 46.87, "longitude": -113.99, "timezone": "America/Denver"}
                d.forecast = load("forecast_missoula.json")["structured_content"]["hourly"][12:18]
            else:
                st.escalate(d, "WEATHER", "fatal", "ConnectError")
        elif s.action == "fetch_listens":
            st.apply_listens(d, st.excluded_window(load(listens_name)["payload"]["listens"], NOW))
        elif s.action == "call_radio":
            st.record_radio_call(d, cfg, s.detail.get("default_prompt") or "ambient", pool())
        elif s.action == "select":
            st.run_select(d)
        elif s.action == "verify":
            st.record_verify(d, [m for m in s.detail["mbids"] if m not in bad_mbids],
                             [m for m in s.detail["mbids"] if m in bad_mbids])
        elif s.action == "request_approval":
            d.approved = not decline
        elif s.action == "write":
            d.playlist_mbid = "playlist-1"
        else:
            raise AssertionError(f"driver cannot handle {s}")
    raise AssertionError("did not finish in 40 steps")


class TestGoldenSlice:
    def test_golden_1_order_and_result(self):
        d = st.RunData(city="Missoula")
        trace = drive(d, CFG)
        assert trace == [("WEATHER", "dispatch_weather"), ("CONTEXT", "fetch_listens"),
                         ("CURATE", "call_radio"), ("CURATE", "select"), ("VERIFY", "verify"),
                         ("APPROVE", "request_approval"), ("WRITE", "write"), ("DONE", "created")]
        assert len(d.selection) == 10 and 2700 <= d.select_total_s <= 3300
        recent = set(st.excluded_window(load("listens_covered.json")["payload"]["listens"], NOW)["excluded_mbids"])
        assert not recent & set(d.selection)
        assert st.final_status(d) == "answered"

    def test_golden_1_never_writes_before_approval(self):
        d = st.RunData(city="Missoula")
        trace = [a for _, a in drive(d, CFG)]
        assert trace.index("request_approval") < trace.index("write")

    def test_golden_2_asks_for_the_city_before_any_call(self):
        d = st.RunData()
        assert st.next_step(d, CFG) == st.Step("LOCATION", "ask_city")
        st.record_ask_city(d)
        st.set_city(d, "Missoula")
        trace = drive(d, CFG)
        assert [a for _, a in trace].count("ask_city") == 0 and trace[-1] == ("DONE", "created")

    def test_golden_3_weather_down_builds_from_the_default_prompt(self):
        d = st.RunData(city="Missoula")
        trace = drive(d, CFG, weather="down")
        assert d.weather_failed and d.forecast is None and trace[-1] == ("DONE", "created")
        assert d.tags_used == ["ambient"] and st.final_status(d) == "answered_low_confidence"
        assert st.effective_zone(d, CFG) == "America/Denver"

    def test_unknown_window_still_finishes_and_is_flagged(self):
        d = st.RunData(city="Missoula")
        drive(d, CFG, listens_name="listens_short_span.json")
        assert d.listens["status"] == "unknown" and st.final_status(d) == "answered_low_confidence"

    def test_declined_approval_writes_nothing(self):
        d = st.RunData(city="Missoula")
        trace = drive(d, CFG, decline=True)
        assert trace[-1] == ("DONE", "declined") and "write" not in [a for _, a in trace]
        assert d.playlist_mbid is None and st.final_status(d) == "answered"

    def test_bad_mbid_reselects_and_still_finishes(self):
        probe = st.RunData(city="Missoula", forecast=[], tracks=pool(), listens=st.excluded_window(
            load("listens_covered.json")["payload"]["listens"], NOW))
        st.run_select(probe)
        first = probe.selection[0]
        d = st.RunData(city="Missoula")
        trace = drive(d, CFG, bad_mbids={first})
        assert first not in d.selection and len(d.selection) == 10 and trace[-1] == ("DONE", "created")
        assert d.radio_calls == 1
