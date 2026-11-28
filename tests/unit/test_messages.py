"""Typed messages, boundary validation and the shared run budget (no network, no model)."""
import json

import pytest
from pydantic import ValidationError

import messages as m

ENV = dict(run_id="r1", agent="curator", stage="CURATE")


def err(**kw):
    base = dict(ENV, kind="input", message="bad tag", required=True)
    base.update(kw)
    return m.AgentError(**base)


class TestEnvelope:
    def test_version_defaults_to_current(self):
        assert m.Brief(**ENV, action="dispatch").version == m.SCHEMA_VERSION == 1

    @pytest.mark.parametrize("bad", [0, 2, -1])
    def test_unknown_version_rejected(self, bad):
        with pytest.raises(ValidationError):
            m.Brief(**ENV, action="dispatch", version=bad)

    @pytest.mark.parametrize("field", ["run_id", "agent", "stage"])
    def test_blank_envelope_field_rejected(self, field):
        with pytest.raises(ValidationError):
            m.Brief(**{**ENV, field: ""}, action="dispatch")

    def test_extra_fields_rejected(self):
        with pytest.raises(ValidationError):
            m.Brief(**ENV, action="dispatch", surprise=1)

    def test_messages_are_immutable(self):
        b = m.Brief(**ENV, action="dispatch")
        with pytest.raises(ValidationError):
            b.action = "other"


class TestAgentError:
    @pytest.mark.parametrize("kind", ["transient", "input", "resource", "fatal", "budget_exceeded", "invalid_message"])
    def test_all_six_kinds_accepted(self, kind):
        assert err(kind=kind).kind == kind

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            err(kind="oops")

    def test_message_is_one_line_and_capped(self):
        with pytest.raises(ValidationError):
            err(message="two\nlines")
        with pytest.raises(ValidationError):
            err(message="x" * 301)
        assert err(message="x" * 300)

    def test_required_flag_must_be_given(self):
        with pytest.raises(ValidationError):
            m.AgentError(**ENV, kind="input", message="m")


class TestResult:
    def test_ok_without_error(self):
        assert m.Result(**ENV, status="ok", payload={"n": 1}).error is None

    def test_error_status_needs_an_error(self):
        with pytest.raises(ValidationError):
            m.Result(**ENV, status="error")
        assert m.Result(**ENV, status="error", error=err()).error.kind == "input"

    def test_ok_must_not_carry_an_error(self):
        with pytest.raises(ValidationError):
            m.Result(**ENV, status="ok", error=err())

    def test_partial_may_carry_an_error(self):
        r = m.Result(**ENV, status="partial", error=err(kind="fatal", required=False))
        assert r.error.required is False


class TestCheck:
    def test_accepts_dict_json_and_instance(self):
        d = dict(ENV, action="dispatch")
        assert m.check(d, m.Brief, "send").action == "dispatch"
        assert m.check(json.dumps(d), m.Brief, "receive").action == "dispatch"
        assert m.check(m.Brief(**d), m.Brief, "send").action == "dispatch"

    def test_rejection_is_logged_and_raised_without_values(self):
        events = []
        secret = "SECRET-VALUE-123"
        with pytest.raises(m.MessageRejected) as ei:
            m.check(dict(ENV, action=secret, version=9), m.Brief, "receive", log=lambda e, **f: events.append((e, f)))
        assert events and events[0][0] == "invalid_message"
        f = events[0][1]
        assert f["direction"] == "receive" and f["model"] == "Brief" and "version" in f["reason"]
        assert secret not in json.dumps(events) and secret not in str(ei.value)

    @pytest.mark.parametrize("raw", ["not json", "[1, 2]", "42", None, b"\xff"])
    def test_garbage_is_rejected(self, raw):
        with pytest.raises(m.MessageRejected):
            m.check(raw, m.Brief, "receive")

    def test_bad_direction_is_a_programming_error(self):
        with pytest.raises(ValueError):
            m.check({}, m.Brief, "sideways")

    def test_wrong_model_is_rejected(self):
        with pytest.raises(m.MessageRejected):
            m.check(dict(ENV, action="x"), m.Result, "receive")


class TestScrub:
    def test_one_line_capped_and_token_removed(self):
        s = m.scrub("a\nb  tok123abc " + "z" * 400, secrets=["tok123abc"])
        assert "\n" not in s and "tok123abc" not in s and "***" in s and len(s) == 300

    def test_short_secrets_are_ignored(self):
        assert m.scrub("ab ab", secrets=["ab"]) == "ab ab"


class TestFinalStatus:
    def test_five_values_match_the_midterm_enum(self):
        assert m.FINAL_STATUSES == ("answered", "answered_low_confidence", "stuck", "budget_exceeded", "policy_halt")


class TestRunBudget:
    def test_charge_counts_and_reports_remaining(self):
        b = m.RunBudget(5)
        assert b.charge("model") == 4 and b.charge("tool", 2) == 2
        assert b.used == 3 and b.by_kind == {"model": 1, "tool": 2}

    def test_exceeding_raises_and_does_not_spend(self):
        b = m.RunBudget(3)
        b.charge("verify", 2)
        with pytest.raises(m.BudgetExceeded) as ei:
            b.charge("verify", 2)
        assert b.used == 2 and ei.value.used == 2 and ei.value.limit == 3

    def test_exact_limit_is_allowed_then_exhausted(self):
        b = m.RunBudget(2)
        b.charge("a")
        b.charge("a")
        assert b.exhausted and b.remaining == 0
        with pytest.raises(m.BudgetExceeded):
            b.charge("a")

    @pytest.mark.parametrize("n", [0, -1])
    def test_bad_cost_rejected(self, n):
        with pytest.raises(ValueError):
            m.RunBudget(3).charge("a", n)

    @pytest.mark.parametrize("limit,used", [(0, 0), (-1, 0), (3, -1)])
    def test_bad_construction_rejected(self, limit, used):
        with pytest.raises(ValueError):
            m.RunBudget(limit, used)

    def test_snapshot_round_trips_through_json(self):
        b = m.RunBudget(10)
        b.charge("model", 3)
        b.charge("tool", 2)
        c = m.RunBudget.restore(json.loads(json.dumps(b.snapshot())))
        assert (c.limit, c.used, c.by_kind) == (10, 5, {"model": 3, "tool": 2})

    def test_resume_gets_a_fresh_budget(self):
        b = m.RunBudget(10, used=10)
        f = m.RunBudget.fresh_for_resume(b.limit)
        assert f.used == 0 and f.remaining == 10

    def test_default_is_the_logged_starting_value(self):
        assert m.RunBudget().limit == m.DEFAULT_BUDGET == 60

    def test_budget_error_message_is_a_valid_agent_error(self):
        b = m.RunBudget(1)
        b.charge("model")
        with pytest.raises(m.BudgetExceeded) as ei:
            b.charge("tool")
        e = m.budget_error("r1", "supervisor", "CURATE", ei.value)
        assert e.kind == "budget_exceeded" and e.required is True and "1 of 1" in e.message
