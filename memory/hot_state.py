"""Hot state: a small SQLite key-value store for session state.

Adapted from memory_examples/ch07_06_sqlite_hot_store.py.
Scoped by (user_id, key); values are JSON; optional TTL.
"""

import json
import os
import sqlite3
import time
from typing import Any

# Store the DB next to this file, not relative to cwd, so the agent can be
# run from encore/ or the repo root.
DB_PATH = os.path.join(os.path.dirname(__file__), "hot_state.db")


def open_store(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hot_state (
            user_id    TEXT NOT NULL,
            key        TEXT NOT NULL,
            value      TEXT NOT NULL,   -- JSON
            expires_at REAL,            -- unix time, NULL = never
            PRIMARY KEY (user_id, key)
        )
        """
    )
    conn.commit()
    return conn


def kv_set(conn: sqlite3.Connection, user_id: str, key: str, value: Any,
           ttl_s: float | None = None) -> None:
    expires_at = time.time() + ttl_s if ttl_s else None
    conn.execute(
        "INSERT OR REPLACE INTO hot_state VALUES (?, ?, ?, ?)",
        (user_id, key, json.dumps(value), expires_at),
    )
    conn.commit()


def kv_get(conn: sqlite3.Connection, user_id: str, key: str,
           default: Any = None) -> Any:
    row = conn.execute(
        "SELECT value, expires_at FROM hot_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    if row is None:
        return default
    value, expires_at = row
    if expires_at is not None and expires_at < time.time():
        kv_delete(conn, user_id, key)
        return default
    return json.loads(value)


def kv_delete(conn: sqlite3.Connection, user_id: str, key: str) -> None:
    conn.execute(
        "DELETE FROM hot_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    )
    conn.commit()


def kv_forget_user(conn: sqlite3.Connection, user_id: str) -> int:
    cur = conn.execute("DELETE FROM hot_state WHERE user_id = ?", (user_id,))
    conn.commit()
    return cur.rowcount