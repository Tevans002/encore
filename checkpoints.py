"""Durable checkpoints for Forecast FM, keyed by run_id and stage (docs/decisions.md, Part 5.1).

Pure module (stdlib sqlite3, no encore import). The driver passes the database path, the clock and the
stage order. Rules it enforces:
  * one row per (run_id, stage); a stage that is "done" is never saved again (completed stages are never replayed)
  * a stage may be saved as "started" before a side effect, "failed" after an error, "done" when finished
  * --resume restarts from the first stage that is not done
  * a stage left "started" means a side effect may have happened: the driver must run its duplicate check first
  * nothing is stored if the payload holds a deny string (token, ListenBrainz user) or is not JSON
  * clear_all() removes everything, for --forget
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

STATUSES = ("started", "failed", "done")
MAX_PAYLOAD_BYTES = 1_000_000
_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
    seq INTEGER NOT NULL, payload TEXT NOT NULL, saved_at TEXT NOT NULL,
    PRIMARY KEY (run_id, stage)
)"""


class CheckpointError(Exception):
    pass


class UnknownRun(CheckpointError):
    pass


class AlreadyDone(CheckpointError):
    """A completed stage was saved again."""


class Rejected(CheckpointError):
    """The payload was refused (deny string, not JSON, too large). Nothing was stored."""


def new_run_id(now_iso: str, suffix: str) -> str:
    """e.g. new_run_id('2026-11-27T14:15:00', 'a1b2') -> 'r-20261127T141500-a1b2'. The caller supplies both."""
    stamp = "".join(ch for ch in now_iso[:19] if ch.isalnum())
    if not suffix.isalnum():
        raise ValueError("suffix must be letters and digits")
    return f"r-{stamp}-{suffix}"


class Store:
    def __init__(self, path: str, deny: Iterable[str] = ()):
        self._deny = [str(s) for s in deny if s and len(str(s)) >= 3]
        self._db = sqlite3.connect(path)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def _next_seq(self, run_id: str) -> int:
        row = self._db.execute("SELECT COALESCE(MAX(seq), 0) FROM checkpoints WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] + 1

    def save(self, run_id: str, stage: str, status: str, payload: dict, now_iso: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if not run_id or not stage:
            raise ValueError("run_id and stage are required")
        try:
            text = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError):
            raise Rejected("payload is not JSON-serializable") from None
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise Rejected("payload is too large")
        low = text.lower()
        if any(s.lower() in low for s in self._deny):
            raise Rejected("payload holds a deny string")
        row = self._db.execute("SELECT status, seq FROM checkpoints WHERE run_id = ? AND stage = ?",
                               (run_id, stage)).fetchone()
        if row and row[0] == "done":
            raise AlreadyDone(f"{stage} is already done for {run_id}; completed stages are never replayed")
        seq = row[1] if row else self._next_seq(run_id)
        with self._db:
            self._db.execute(
                "INSERT INTO checkpoints (run_id, stage, status, seq, payload, saved_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(run_id, stage) DO UPDATE SET status=excluded.status, payload=excluded.payload, "
                "saved_at=excluded.saved_at", (run_id, stage, status, seq, text, now_iso))

    def _rows(self, run_id: str) -> list:
        rows = self._db.execute("SELECT stage, status, seq, payload, saved_at FROM checkpoints "
                                "WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        if not rows:
            raise UnknownRun(f"no checkpoints for {run_id}")
        return rows

    def load(self, run_id: str, stage: str) -> Optional[dict]:
        row = self._db.execute("SELECT status, payload, saved_at FROM checkpoints WHERE run_id = ? AND stage = ?",
                               (run_id, stage)).fetchone()
        if not row:
            return None
        return {"status": row[0], "payload": json.loads(row[1]), "saved_at": row[2]}

    def statuses(self, run_id: str) -> dict:
        return {r[0]: r[1] for r in self._rows(run_id)}

    def latest_done(self, run_id: str) -> Optional[dict]:
        """The payload of the most recently completed stage, or None if no stage is done."""
        done = [r for r in self._rows(run_id) if r[1] == "done"]
        if not done:
            return None
        r = done[-1]
        return {"stage": r[0], "payload": json.loads(r[3]), "saved_at": r[4]}

    def resume_plan(self, run_id: str, order: Iterable[str]) -> dict:
        """Which stages to skip and which to run. Restarts from the first stage that is not done."""
        status = self.statuses(run_id)
        order = list(order)
        unknown = [s for s in status if s not in order]
        if unknown:
            raise CheckpointError(f"stages not in the order: {unknown}")
        first = next((s for s in order if status.get(s) != "done"), None)
        if first is None:
            return {"skip": order, "run": [], "first": None, "check_duplicates": False}
        i = order.index(first)
        later_done = [s for s in order[i + 1:] if status.get(s) == "done"]
        if later_done:
            raise CheckpointError(f"stage {first} is not done but later stages are: {later_done}")
        return {"skip": order[:i], "run": order[i:], "first": first,
                "check_duplicates": status.get(first) == "started"}

    def clear_run(self, run_id: str) -> int:
        with self._db:
            return self._db.execute("DELETE FROM checkpoints WHERE run_id = ?", (run_id,)).rowcount

    def clear_all(self) -> int:
        """For --forget. Returns the number of rows removed."""
        with self._db:
            n = self._db.execute("DELETE FROM checkpoints").rowcount
        self._db.execute("VACUUM")
        return n

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]

    def runs(self) -> list:
        return [r[0] for r in self._db.execute("SELECT DISTINCT run_id FROM checkpoints ORDER BY run_id")]
