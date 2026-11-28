"""Thin supervisor (Part 5A) for Forecast FM: bounded choices, briefs, synthesis, topology.

Pure module: no network, no model, no encore import. The LLM supervisor calls no tools and sees no raw tool
output. For each state code lists the options; the model picks one by id; code validates the pick and falls
back to the first option if the model cannot give a valid one. Code, not the model, builds every brief's data
(track lists, MBIDs, allowed prompts). Decisions: docs/decisions.md ("States and workers", "Run budget and
structured errors"). In a Brief or Result the `agent` field is the worker involved.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import messages as msg
import states as st

AGENT = "supervisor"
WORKERS = {"weather": False, "curator": True, "publisher": True}      # name -> required
STATE_WORKER = {"WEATHER": "weather", "CURATE": "curator", "APPROVE": "publisher", "WRITE": "publisher"}
APPROVAL_OWNER = "publisher"        # the one agent that requests approval
MEMORY_OWNER = "publisher"          # the one agent that writes episodic memory
NOTE_CHARS = 300
RESERVED_DATA_KEYS = frozenset({"allowed_prompts", "preferred_tag", "reflections"})   # set only by build_brief
MAX_CHOICE_TRIES = 2
REASONS = frozenset({"no_tracks", "no_set_in_window", "too_few_usable", "verify_failed"})
SUPERVISOR_SYSTEM_PROMPT = (
    "You are the supervisor of a music agent. You call no tools. Choose exactly one of the listed options by "
    "its id. Reply with one JSON object only: {\"option_id\": \"...\", \"tag\": \"optional, from the listed "
    "tags\", \"note\": \"optional, one short line\"}. Never invent an option or a tag."
)


@dataclass(frozen=True)
class Option:
    id: str
    worker: Optional[str]         # None: the user is asked, or the run stops
    action: str
    prompts: tuple = ()           # radio prompts the curator may use


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    option_id: str = Field(min_length=1, max_length=40)
    tag: Optional[str] = Field(default=None, max_length=100)
    note: str = Field(default="", max_length=NOTE_CHARS)


class ChoiceRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Decision:
    option: Option
    choice: Optional[Choice]
    how: str          # chosen, fallback or forced
    tries: int


def worker_tools(worker: str) -> tuple:
    return tuple(t for s, w in STATE_WORKER.items() if w == worker for t in st.TOOLS[s])


def worker_may_use(worker: str, state: str, tool: str) -> bool:
    """The supervisor has no tools, and states with no worker (LOCATION, CONTEXT, VERIFY) belong to code."""
    return STATE_WORKER.get(state) == worker and tool in st.TOOLS[state]


def may_request_approval(agent: str) -> bool:
    return agent == APPROVAL_OWNER


def may_write_memory(agent: str) -> bool:
    return agent == MEMORY_OWNER


def route_ok(sender: str, receiver: str) -> bool:
    """A DAG: orchestrators (the supervisor and code) message workers, workers reply to them, never to each other."""
    if sender in (AGENT, "code"):
        return receiver in WORKERS
    return sender in WORKERS and receiver in (AGENT, "code")


def options_for(step: st.Step, cfg: st.Config) -> list:
    """The bounded choices for this step. Empty when code runs the step itself or the run is over."""
    if step.state in ("DONE", "STOP"):
        return []
    a, det = step.action, step.detail
    if a == "dispatch_weather":
        return [Option(a, "weather", a)]                # optional state: no stop option
    if a in ("request_approval", "write"):
        opts = [Option(a, "publisher", a)]
    elif a == "call_radio":
        prompts = (det["default_prompt"],) if det.get("default_prompt") else \
            tuple(det.get("artist_prompts", ())) + tuple(cfg.table_tags)
        opts = [Option(a, "curator", a, prompts)]
    elif a == "recall_same_tag":
        opts = [Option(a, "curator", a, (det["tag"],))]
    elif a == "different_tag":
        opts = [Option(a, "curator", a, tuple(det.get("candidates", ())) + tuple(det.get("artist_prompts", ())))]
    elif a == "ask_user":
        opts = [Option(a, None, a)]
    else:
        return []                                        # ask_city, fetch_listens, select, verify: code runs them
    if step.state not in st.OPTIONAL_STATES:
        opts.append(Option("stop", None, "stop"))
    return opts


def safe_summary(d: st.RunData) -> dict:
    """What the supervisor may see: counts and fixed words only. No titles, names or server text."""
    status = (d.listens or {}).get("status")
    return {"radio_calls": int(d.radio_calls),
            "attempts": {k: int(v) for k, v in d.attempts.items() if k in st.STATES},
            "select_reason": d.select_reason if d.select_reason in REASONS else ("ok" if d.select_reason == "" else None),
            "weather_failed": bool(d.weather_failed), "listens": status if status in ("covered", "unknown") else None,
            "has_selection": d.selection is not None, "window_relaxed": bool(d.window_relaxed)}


def build_choice_request(step: st.Step, options: list, summary: dict) -> dict:
    lines = [f"state: {step.state}", f"action: {step.action}", "summary: " + json.dumps(summary, sort_keys=True), "options:"]
    for o in options:
        lines.append(f"- {o.id} (worker: {o.worker or 'none'})" + (f" tags: {', '.join(o.prompts)}" if o.prompts else ""))
    return {"system": SUPERVISOR_SYSTEM_PROMPT, "user": "\n".join(lines)}


def _json_object(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if not isinstance(text, str):
        raise ChoiceRejected("reply is not text")
    try:
        data = json.loads(text)
    except ValueError:
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j <= i:
            raise ChoiceRejected("no JSON object in the reply") from None
        try:
            data = json.loads(text[i:j + 1])
        except ValueError:
            raise ChoiceRejected("reply is not valid JSON") from None
    if not isinstance(data, dict):
        raise ChoiceRejected("reply is not a JSON object")
    return data


def parse_choice(raw: Any, options: list) -> tuple:
    """Validate the model's reply against this step's options. Returns (option, choice) or raises ChoiceRejected."""
    data = dict(_json_object(raw))
    if isinstance(data.get("note"), str):
        data["note"] = msg.scrub(data["note"], limit=NOTE_CHARS)
    try:
        choice = Choice.model_validate(data)
    except ValidationError as exc:
        names = ",".join(sorted({".".join(map(str, e["loc"])) or "(root)" for e in exc.errors()}))
        raise ChoiceRejected(f"invalid fields: {names}") from None
    opt = next((o for o in options if o.id == choice.option_id), None)
    if opt is None:
        raise ChoiceRejected("option_id is not offered")
    if choice.tag is not None and choice.tag not in opt.prompts:
        raise ChoiceRejected("tag is not offered")
    return opt, choice


def _check_ctx(ctx: dict) -> None:
    missing = [k for k in ("run_id", "agent", "stage") if not ctx.get(k)]
    if missing:
        raise ValueError(f"ctx needs {missing}")


def decide(options: list, request: dict, choose_fn: Callable, *, ctx: dict, trace: Optional[Callable] = None,
           charge: Optional[Callable] = None, max_tries: int = MAX_CHOICE_TRIES) -> Decision:
    """One option is forced without a model call. Otherwise ask up to max_tries times (1 budget unit each),
    then fall back to the first option. A BudgetExceeded from `charge` is not caught: the driver stops."""
    if not options:
        raise ValueError("no options to choose from")
    _check_ctx(ctx)
    emit = trace or (lambda event, **fields: None)
    if len(options) == 1:
        emit("agent_msg", **ctx, direction="decide", verdict="forced", option=options[0].id)
        return Decision(options[0], None, "forced", 0)
    for n in range(1, max_tries + 1):
        if charge:
            charge("supervisor")
        try:
            opt, choice = parse_choice(choose_fn(request), options)
        except ChoiceRejected as exc:
            reason = exc.reason
        except Exception as exc:
            reason = f"choose_fn failed: {type(exc).__name__}"
        else:
            emit("agent_msg", **ctx, direction="receive", verdict="accepted", option=opt.id, attempt=n)
            return Decision(opt, choice, "chosen", n)
        emit("agent_msg", **ctx, direction="receive", verdict="rejected", reason=reason, attempt=n)
    emit("agent_msg", **ctx, direction="decide", verdict="fallback", option=options[0].id)
    return Decision(options[0], None, "fallback", max_tries)


def build_brief(run_id: str, stage: str, decision: Decision, *, data: Optional[dict] = None, reflections=()) -> msg.Brief:
    """The Brief for the chosen worker. `data` comes from code; the model's note is the only free text."""
    opt = decision.option
    if opt.worker is None:
        raise ValueError("this option has no worker")
    payload = dict(data or {})
    reserved = RESERVED_DATA_KEYS & set(payload)
    if reserved:
        raise ValueError(f"brief data may not set {sorted(reserved)}")
    if opt.prompts:
        payload["allowed_prompts"] = list(opt.prompts)
    if decision.choice and decision.choice.tag:
        payload["preferred_tag"] = decision.choice.tag
    if reflections:
        payload["reflections"] = list(reflections)
    try:
        json.dumps(payload)
    except (TypeError, ValueError):
        raise ValueError("brief data must be JSON-serializable") from None
    return msg.check({"run_id": run_id, "agent": opt.worker, "stage": stage, "action": opt.action,
                      "instruction": decision.choice.note if decision.choice else "", "data": payload},
                     msg.Brief, "send")


@dataclass
class Synthesis:
    ok: bool
    partial: bool = False
    missing_optional: list = field(default_factory=list)
    errors: list = field(default_factory=list)          # AgentErrors from optional workers
    abort: Optional[msg.AgentError] = None               # set when a required worker failed or did not report
    payloads: dict = field(default_factory=dict)


def _error(run_id, agent, stage, kind, text, required):
    return msg.AgentError(run_id=run_id, agent=agent, stage=stage, kind=kind, message=msg.scrub(text), required=required)


def synthesize(run_id: str, stage: str, results: dict, expected: dict, log: Optional[Callable] = None) -> Synthesis:
    """Check that every expected worker reported. A missing or failed optional worker gives a partial result;
    a missing, malformed or failed required worker aborts with a structured error."""
    unknown = [w for w in expected if w not in WORKERS]
    if unknown:
        raise ValueError(f"unknown workers: {unknown}")
    out = Synthesis(ok=True)
    for name, required in expected.items():
        raw = results.get(name)
        if raw is None:
            if required:
                out.abort = out.abort or _error(run_id, name, stage, "fatal", f"required worker {name} did not report", True)
            else:
                out.missing_optional.append(name)
                out.partial = True
            continue
        try:
            r = msg.check(raw, msg.Result, "receive", log)
            if r.run_id != run_id or r.agent != name:
                raise msg.MessageRejected("receive", "Result", "run_id or agent does not match the brief")
        except msg.MessageRejected as exc:
            err = _error(run_id, name, stage, "invalid_message", exc.reason, required)
            if required:
                out.abort = out.abort or err
            else:
                out.errors.append(err)
                out.partial = True
            continue
        if r.status == "error":
            if required:
                out.abort = out.abort or r.error
            else:
                out.errors.append(r.error)
                out.partial = True
            continue
        if r.status == "partial":
            out.partial = True
            if r.error:
                out.errors.append(r.error)
        out.payloads[name] = r.payload
    out.ok = out.abort is None
    return out
