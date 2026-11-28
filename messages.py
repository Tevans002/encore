"""Typed messages and the shared run budget for Forecast FM (docs/decisions.md, "Run budget and structured errors").

No imports from encore: tools, clocks and trace functions are passed in by the caller.
Every inter-agent message is a Pydantic model with a `version` field. `check()` validates a message on
send and on receive; a malformed one raises MessageRejected and is reported through the optional `log`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 1
MESSAGE_CHARS = 300

FinalStatus = Literal["answered", "answered_low_confidence", "stuck", "budget_exceeded", "policy_halt"]
FINAL_STATUSES = ("answered", "answered_low_confidence", "stuck", "budget_exceeded", "policy_halt")
ErrorKind = Literal["transient", "input", "resource", "fatal", "budget_exceeded", "invalid_message"]
ResultStatus = Literal["ok", "partial", "error"]

DEFAULT_BUDGET = 60   # starting value, a guess; calibrate from real traces (decisions.md)


def scrub(text: Any, secrets=(), limit: int = MESSAGE_CHARS) -> str:
    """One line, secrets replaced by ***, capped at `limit` characters."""
    s = str(text)
    for secret in secrets:
        if secret and len(str(secret)) >= 3:
            s = s.replace(str(secret), "***")
    return " ".join(s.split())[:limit]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Envelope(_Msg):
    version: int = SCHEMA_VERSION
    run_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    stage: str = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def _known_version(cls, v: int) -> int:
        if v != SCHEMA_VERSION:
            raise ValueError(f"unsupported message version {v}; this code reads {SCHEMA_VERSION}")
        return v


class AgentError(Envelope):
    kind: ErrorKind
    message: str = Field(max_length=MESSAGE_CHARS)
    required: bool
    tool: Optional[str] = None

    @field_validator("message")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if "\n" in v or "\r" in v:
            raise ValueError("message must be one line")
        return v


class Brief(Envelope):
    """Supervisor to worker. `action` must be one of the options code listed for the stage."""
    action: str = Field(min_length=1)
    instruction: str = Field(default="", max_length=1000)
    data: dict = Field(default_factory=dict)       # typed payloads (track lists, MBIDs) are passed by code


class Result(Envelope):
    """Worker to supervisor."""
    status: ResultStatus
    payload: dict = Field(default_factory=dict)
    error: Optional[AgentError] = None

    @model_validator(mode="after")
    def _error_matches_status(self):
        if self.status == "error" and self.error is None:
            raise ValueError("status 'error' needs an error")
        if self.status == "ok" and self.error is not None:
            raise ValueError("status 'ok' must not carry an error")
        return self


class MessageRejected(Exception):
    """A message failed validation at a boundary."""

    def __init__(self, direction: str, model: str, reason: str):
        super().__init__(f"{direction} {model} rejected: {reason}")
        self.direction, self.model, self.reason = direction, model, reason


def check(raw: Any, model: type, direction: str, log: Optional[Callable[..., None]] = None):
    """Validate `raw` (a dict, a JSON string or an instance) as `model`. direction: 'send' or 'receive'.

    Returns the validated message. On failure calls log(event, **fields) if given, then raises
    MessageRejected. The reason is one line and capped, and holds no input values.
    """
    if direction not in ("send", "receive"):
        raise ValueError("direction must be 'send' or 'receive'")
    try:
        if isinstance(raw, model):
            return model.model_validate(raw.model_dump())
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(data, dict):
            raise ValueError("message must be a JSON object")
        return model.model_validate(data)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            reason = "; ".join(f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['type']}" for e in exc.errors()[:5])
        else:
            reason = type(exc).__name__
        reason = scrub(reason)
        if log:
            log("invalid_message", direction=direction, model=model.__name__, reason=reason)
        raise MessageRejected(direction, model.__name__, reason) from None


class BudgetExceeded(Exception):
    def __init__(self, used: int, limit: int, what: str):
        super().__init__(f"run budget exhausted: {used} of {limit} units used (next: {what})")
        self.used, self.limit, self.what = used, limit, what


class RunBudget:
    """One counter for the whole run, shared by every agent. Code owns it; the model never sees it.

    charge() raises BudgetExceeded and does NOT spend when the cost would pass the limit, so the
    caller stops instead of retrying. Costs: 1 per model call, 1 per tool call, verify costs 1 per MBID.
    """

    def __init__(self, limit: int = DEFAULT_BUDGET, used: int = 0):
        if limit < 1 or used < 0:
            raise ValueError("limit must be at least 1 and used must not be negative")
        self.limit, self.used = limit, used
        self.by_kind: dict = {}

    @property
    def remaining(self) -> int:
        return max(self.limit - self.used, 0)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def charge(self, kind: str, n: int = 1) -> int:
        if n < 1:
            raise ValueError("cost must be at least 1")
        if self.used + n > self.limit:
            raise BudgetExceeded(self.used, self.limit, kind)
        self.used += n
        self.by_kind[kind] = self.by_kind.get(kind, 0) + n
        return self.remaining

    def snapshot(self) -> dict:
        return {"limit": self.limit, "used": self.used, "by_kind": dict(self.by_kind)}

    @classmethod
    def restore(cls, snap: dict) -> "RunBudget":
        b = cls(int(snap["limit"]), int(snap["used"]))
        b.by_kind = dict(snap.get("by_kind", {}))
        return b

    @classmethod
    def fresh_for_resume(cls, limit: int = DEFAULT_BUDGET) -> "RunBudget":
        """--resume starts the remaining states with a fresh budget (decisions.md)."""
        return cls(limit)


def budget_error(run_id: str, agent: str, stage: str, exc: BudgetExceeded, required: bool = True) -> AgentError:
    return AgentError(run_id=run_id, agent=agent, stage=stage, kind="budget_exceeded",
                      message=scrub(exc), required=required)
