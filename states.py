"""Step machine for Forecast FM: states, completion checks, attempt bounds, tool gate, escalation.

Pure module (docs/decisions.md: "States and workers", "Escalation when no set fits", "Final statuses").
It never imports encore: the driver passes data in and performs the actions this module names.
Code decides completion and advancement; completion checks test what is KNOWN, not what was asked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Iterable, Optional

import track_select as ts

STATES = ("LOCATION", "WEATHER", "CONTEXT", "CURATE", "VERIFY", "APPROVE", "WRITE")
OPTIONAL_STATES = frozenset({"WEATHER"})
TOOLS = {
    "LOCATION": (),
    "WEATHER": ("openmeteo_search_locations", "openmeteo_get_forecast"),
    "CONTEXT": ("get_recent_listens",),
    "CURATE": ("lb_radio",),
    "VERIFY": ("verify_recordings",),
    "APPROVE": (),
    "WRITE": ("create_playlist", "note_preference"),
}
WRITE_TOOLS = frozenset({"create_playlist", "note_preference"})
MAX_ATTEMPTS = 3            # per state, counting Reflexion revisions (starting value)
MAX_RADIO_CALLS = 3         # per run, first call included (student-approved)
MAX_VERIFY_RESELECTS = 2    # re-selects after failed verification, per run
MAX_VIOLATIONS = 2          # second out-of-list or pre-approval request ends the run (policy_halt)
EXCLUDE_DAYS = 3
LISTENS_REQUESTED = 100
RELAXED_WINDOW = (2400, 3600)


@dataclass(frozen=True)
class Config:
    """Inputs the design has not fixed yet are required, with no defaults."""
    table_tags: tuple
    fallback_zone: str
    default_prompt: str

    def __post_init__(self):
        if not self.table_tags or len(set(self.table_tags)) != len(self.table_tags):
            raise ValueError("table_tags must be a non-empty list without repeats")
        if self.default_prompt not in self.table_tags:
            raise ValueError("default_prompt must be one of the live-checked table tags")
        if not self.fallback_zone:
            raise ValueError("fallback_zone is required")


@dataclass
class RunData:
    run_id: str = ""
    city: Optional[str] = None
    asked_city: bool = False
    location: Optional[dict] = None       # {"latitude", "longitude", "timezone"}
    forecast: Optional[list] = None       # hourly rows for the requested hours
    weather_failed: bool = False
    listens: Optional[dict] = None        # result of excluded_window()
    tracks: list = field(default_factory=list)   # current radio pool, in radio order
    radio_calls: int = 0
    tags_used: list = field(default_factory=list)
    same_tag_retried: bool = False
    other_tag_tried: bool = False
    invalid_mbids: list = field(default_factory=list)
    verified: list = field(default_factory=list)
    verify_reselects: int = 0
    selection: Optional[list] = None      # MBIDs in rank order
    select_total_s: Optional[int] = None
    select_reason: Optional[str] = None   # None = not tried, "" = ok, else a failure reason
    window: list = field(default_factory=lambda: [ts.LO_S, ts.HI_S])
    window_relaxed: bool = False
    approved: Optional[bool] = None
    playlist_mbid: Optional[str] = None
    attempts: dict = field(default_factory=dict)
    violations: int = 0
    halted: bool = False
    stop: Optional[dict] = None           # {"state", "kind", ...}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunData":
        names = {f.name for f in fields(cls)}
        extra = set(data) - names
        if extra:
            raise ValueError(f"unknown fields: {sorted(extra)}")
        return cls(**data)


@dataclass(frozen=True)
class Step:
    state: str            # a state, or "DONE" / "STOP"
    action: str
    detail: dict = field(default_factory=dict)


class RadioCapReached(Exception):
    pass


def track_mbid(t: dict) -> str:
    if t.get("recording_mbid"):
        return t["recording_mbid"]
    ident = t.get("identifier")
    ident = ident[0] if isinstance(ident, list) else ident
    return str(ident).rstrip("/").rsplit("/", 1)[-1]


def offered_tools(state: str) -> tuple:
    return TOOLS[state]


def gate_check(d: RunData, state: str, tool: str) -> str:
    """'allow', 'reject' (first violation) or 'halt' (second: the run ends as policy_halt)."""
    if tool in TOOLS[state] and not (tool in WRITE_TOOLS and d.approved is not True):
        return "allow"
    d.violations += 1
    if d.violations >= MAX_VIOLATIONS:
        d.halted = True
        return "halt"
    return "reject"


def _mbid_of(listen: dict) -> Optional[str]:
    return ((listen.get("track_metadata") or {}).get("mbid_mapping") or {}).get("recording_mbid")


def excluded_window(listens: list, now_ts: int, requested: int = LISTENS_REQUESTED,
                    days: int = EXCLUDE_DAYS) -> dict:
    """Which MBIDs were played in the last `days` days, and whether the fetch reaches that far back.

    'covered' if the oldest fetched listen is at or before the cutoff, or fewer than `requested`
    came back (the whole history was fetched). Otherwise 'unknown'.
    """
    cutoff = now_ts - days * 86400
    inside = [x for x in listens if x.get("listened_at", 0) >= cutoff]
    mbids = []
    for x in inside:
        m = _mbid_of(x)
        if m and m not in mbids:
            mbids.append(m)
    oldest = min((x.get("listened_at", 0) for x in listens), default=None)
    covered = len(listens) < requested or (oldest is not None and oldest <= cutoff)
    return {"status": "covered" if covered else "unknown", "excluded_mbids": mbids,
            "in_window": len(inside), "unmapped": sum(1 for x in inside if not _mbid_of(x))}


def set_city(d: RunData, city: str) -> None:
    city = (city or "").strip()
    if not city:
        raise ValueError("city must not be empty")
    d.city = city


def record_ask_city(d: RunData) -> None:
    d.asked_city = True


def apply_listens(d: RunData, window: dict) -> None:
    d.listens = window


def effective_zone(d: RunData, cfg: Config) -> str:
    return (d.location or {}).get("timezone") or cfg.fallback_zone


def record_attempt(d: RunData, state: str) -> int:
    """Count one attempt (a worker loop or a Reflexion revision). Returns attempts left."""
    d.attempts[state] = d.attempts.get(state, 0) + 1
    return max(MAX_ATTEMPTS - d.attempts[state], 0)


def attempts_exhausted(d: RunData, state: str) -> bool:
    return d.attempts.get(state, 0) >= MAX_ATTEMPTS


def escalate(d: RunData, state: str, kind: str, message: str = "") -> None:
    """Apply the logged escalation for a state that cannot complete."""
    if state == "WEATHER":
        d.weather_failed = True            # optional: continue with the default prompt, never invent a forecast
    elif state == "CONTEXT":
        d.listens = {"status": "unknown", "excluded_mbids": [], "in_window": 0, "unmapped": 0}
    else:
        d.stop = {"state": state, "kind": kind, "message": message[:300]}


def record_radio_call(d: RunData, cfg: Config, tag: str, tracks: Optional[list] = None) -> str:
    """Charge one radio call. `tracks` is None when the call failed. Returns 'first', 'same' or 'different'."""
    if d.radio_calls >= MAX_RADIO_CALLS:
        raise RadioCapReached(f"at most {MAX_RADIO_CALLS} radio calls per run")
    if tag not in cfg.table_tags:
        raise ValueError("tag is not in the live-checked table")      # a Reflexion trigger
    if not d.tags_used:
        kind = "first"
    elif tag == d.tags_used[-1] and not d.same_tag_retried:
        kind = "same"
        d.same_tag_retried = True
    elif tag not in d.tags_used and not d.other_tag_tried:
        kind = "different"
        d.other_tag_tried = True
    else:
        raise ValueError("this tag call is not a step of the escalation ladder")
    d.radio_calls += 1
    d.tags_used.append(tag)
    if tracks is not None:
        d.tracks = list(tracks)            # a new pool replaces the old one; pools are never merged
        d.selection = None
        d.select_reason = None
    return kind


def _blocked(d: RunData) -> set:
    bad = set(d.invalid_mbids) | set((d.listens or {}).get("excluded_mbids", []))
    return {i for i, t in enumerate(d.tracks) if track_mbid(t) in bad}


def run_select(d: RunData) -> dict:
    r = ts.select_tracks(d.tracks, lo=d.window[0], hi=d.window[1], blocked=_blocked(d))
    if r["ok"]:
        d.selection = [track_mbid(d.tracks[i]) for i in r["indices"]]
        d.select_total_s, d.select_reason = r["total_s"], ""
    else:
        d.selection, d.select_total_s, d.select_reason = None, None, r["reason"]
    return r


def record_verify(d: RunData, valid: Iterable[str], invalid: Iterable[str]) -> None:
    for m in valid:
        if m not in d.verified:
            d.verified.append(m)
    bad = [m for m in invalid if m not in d.invalid_mbids]
    d.invalid_mbids.extend(bad)
    if d.selection and any(m in d.invalid_mbids for m in d.selection):
        d.selection, d.select_total_s = None, None
        d.verify_reselects += 1
        d.select_reason = None if d.verify_reselects <= MAX_VERIFY_RESELECTS else "verify_failed"


def accept_relaxed_window(d: RunData) -> None:
    """Only after the user agrees. Code never relaxes the window on its own."""
    if d.window_relaxed:
        raise ValueError("the window was already relaxed once")
    d.window, d.window_relaxed = list(RELAXED_WINDOW), True
    d.selection, d.select_reason = None, None


def decline_relaxed_window(d: RunData) -> None:
    d.stop = {"state": "CURATE", "kind": "stuck", "message": "no set in the window; user declined a wider one"}


def _ladder(d: RunData, cfg: Config) -> Step:
    why = d.select_reason or "no_tracks"
    if d.radio_calls < MAX_RADIO_CALLS:
        if d.tags_used and not d.same_tag_retried:
            return Step("CURATE", "recall_same_tag", {"tag": d.tags_used[-1], "reason": why})
        if not d.other_tag_tried:
            cands = [t for t in cfg.table_tags if t not in d.tags_used]
            if cands:
                return Step("CURATE", "different_tag", {"candidates": cands, "reason": why})
    if d.window_relaxed:
        return Step("STOP", "stuck", {"state": "CURATE", "reason": why})
    return Step("CURATE", "ask_user", {"reason": why, "offer": "relax_window", "window": list(RELAXED_WINDOW)})


def next_step(d: RunData, cfg: Config) -> Step:
    """The first state that is not complete, and what to do in it. Never mutates `d`."""
    if d.halted:
        return Step("STOP", "policy_halt")
    if d.stop:
        return Step("STOP", d.stop.get("kind", "stuck"), dict(d.stop))
    if not d.city:
        if d.asked_city:
            return Step("STOP", "stuck", {"state": "LOCATION", "reason": "no city after one question"})
        return Step("LOCATION", "ask_city")
    if d.forecast is None and not d.weather_failed:
        return Step("WEATHER", "dispatch_weather")
    if d.listens is None:
        return Step("CONTEXT", "fetch_listens")
    if d.selection is None:
        if not d.tracks:
            if d.radio_calls >= MAX_RADIO_CALLS:
                return _ladder(d, cfg)
            prompt = cfg.default_prompt if d.weather_failed else None
            return Step("CURATE", "call_radio", {"default_prompt": prompt})
        if d.select_reason is None:
            return Step("CURATE", "select")
        return _ladder(d, cfg)
    todo = [m for m in d.selection if m not in d.verified]
    if todo:
        return Step("VERIFY", "verify", {"mbids": todo})
    if d.approved is None:
        return Step("APPROVE", "request_approval", {"mbids": list(d.selection)})
    if d.approved is False:
        return Step("DONE", "declined")
    if d.playlist_mbid is None:
        return Step("WRITE", "write", {"mbids": list(d.selection)})
    return Step("DONE", "created")


def final_status(d: RunData) -> Optional[str]:
    """One of the five midterm statuses, or None while the run is still going."""
    if d.halted:
        return "policy_halt"
    if d.stop:
        return "budget_exceeded" if d.stop.get("kind") == "budget_exceeded" else "stuck"
    if d.approved is False:
        return "answered"
    if d.playlist_mbid:
        degraded = d.weather_failed or (d.listens or {}).get("status") == "unknown"
        return "answered_low_confidence" if degraded else "answered"
    if not d.city and d.asked_city:
        return "stuck"
    return None
