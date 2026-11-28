"""Clean stored preferences before they reach the curator (option A).

Pure module: no encore import, no model. Inputs are plain lists that code has already read:
session_preferences (hot state), key_facts (from retrieve_episodic) and long_term_preferences (hot state).
Stored text is free text from earlier sessions, so it is data, never instructions.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

MAX_PREFS = 5
PREF_CHARS = 200
TAG = "stored_preferences"
_TAG_RE = re.compile(rf"</?{TAG}>", re.I)


def _items(x: Any) -> list:
    if x is None:
        return []
    if isinstance(x, str):          # Chroma stores lists as CSV strings
        return [p for p in x.split(",")]
    if isinstance(x, (list, tuple)):
        return list(x)
    return []


def _one_line(text: str, secrets, limit: int) -> str:
    s = text
    while True:                      # strip our delimiter until none is left (nested tags)
        t = _TAG_RE.sub("", s)
        if t == s:
            break
        s = t
    for secret in secrets:
        if secret and len(str(secret)) >= 3:
            s = s.replace(str(secret), "***")
    return " ".join(s.split())[:limit]


def clean_preferences(session: Any = (), episodic: Any = (), long_term: Any = (), *,
                      cap: int = MAX_PREFS, chars: int = PREF_CHARS, secrets: Iterable = ()) -> list:
    """At most `cap` one-line strings of at most `chars` characters.

    Priority: session, then episodic key_facts, then long_term. Non-strings and empties are dropped;
    duplicates are dropped case-insensitively across sources.
    """
    if cap < 0 or chars < 1:
        raise ValueError("cap must be >= 0 and chars >= 1")
    secrets = tuple(secrets)
    out, seen = [], set()
    for source in (session, episodic, long_term):
        for item in _items(source):
            if not isinstance(item, str):
                continue
            s = _one_line(item, secrets, chars)
            if not s or s.casefold() in seen:
                continue
            if len(out) >= cap:
                return out
            seen.add(s.casefold())
            out.append(s)
    return out


def wrap_preferences(prefs: list) -> str:
    """The delimited block the model sees. Clean the strings first; this only adds the delimiters."""
    body = "\n".join(f"- {_one_line(p, (), PREF_CHARS)}" for p in prefs)
    return f"<{TAG}>\n{body}\n</{TAG}>"
