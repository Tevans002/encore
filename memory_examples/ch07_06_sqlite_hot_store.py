"""
Chapter 7 — Memory — Example 6
The Agentic AI Bible (Revised & Expanded Edition 2026)

A straight-shot key-value "hot store" with SQLite.

Hot state is the small, frequently-read state an
agent needs on almost every step of a session:

    - the playlist being edited right now
    - "the last song we talked about"
    - preferences noted this session
    - the tool-call count

It is NOT searched by meaning (that's Chroma) and NOT
about relationships (that's Neo4j). You just look a
value up by its key, fast.

SQLite ships with Python, so there is nothing to
install and no server to run. The whole store is one
file on disk.

This script:
    1. Creates a key-value table
    2. Wraps it in four functions: set, get, delete, forget
    3. Stores JSON values scoped by (user_id, key)
    4. Supports an optional time-to-live (TTL)
    5. Shows the data surviving a "restart"

Run:

    python ch07_06_sqlite_hot_store.py

(Redis alternative at the bottom of this file.)
"""

import json
import sqlite3
import time
from typing import Any


DB_PATH = "ch07_hot_store.db"


#
# 1. One table. The primary key is (user_id, key), so
#    each user has their own namespace and a second
#    set() on the same key overwrites the first.
#

def open_store(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
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


#
# 2. The whole API: four functions.
#

def kv_set(
    conn: sqlite3.Connection,
    user_id: str,
    key: str,
    value: Any,
    ttl_s: float | None = None,
) -> None:
    """Store any JSON-serializable value. Overwrites."""
    expires_at = time.time() + ttl_s if ttl_s else None
    conn.execute(
        "INSERT OR REPLACE INTO hot_state VALUES (?, ?, ?, ?)",
        (user_id, key, json.dumps(value), expires_at),
    )
    conn.commit()


def kv_get(
    conn: sqlite3.Connection,
    user_id: str,
    key: str,
    default: Any = None,
) -> Any:
    """Return the value, or default if missing or expired."""
    row = conn.execute(
        "SELECT value, expires_at FROM hot_state "
        "WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()

    if row is None:
        return default

    value, expires_at = row
    if expires_at is not None and expires_at < time.time():
        kv_delete(conn, user_id, key)   # lazy cleanup
        return default

    return json.loads(value)


def kv_delete(conn: sqlite3.Connection, user_id: str, key: str) -> None:
    conn.execute(
        "DELETE FROM hot_state WHERE user_id = ? AND key = ?",
        (user_id, key),
    )
    conn.commit()


def kv_forget_user(conn: sqlite3.Connection, user_id: str) -> int:
    """Privacy path: delete everything for one user. Returns the count."""
    cur = conn.execute(
        "DELETE FROM hot_state WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    return cur.rowcount


#
# Demo
#

if __name__ == "__main__":

    USER = "alice"
    conn = open_store()

    # 3. Values can be strings, numbers, lists, or dicts.
    kv_set(conn, USER, "active_playlist", {
        "id": "pl-123",
        "title": "[Encore] Focus — Oct 1",
        "track_count": 11,
    })
    kv_set(conn, USER, "last_recording", {
        "mbid": "00000000-0000-0000-0000-000000000000",
        "title": "Night Bus",
    })
    kv_set(conn, USER, "session_preferences", ["no vocals", "no Christmas music"])
    kv_set(conn, USER, "tool_calls", 0)

    # Read-modify-write: bump a counter.
    kv_set(conn, USER, "tool_calls", kv_get(conn, USER, "tool_calls") + 1)

    print("active_playlist   ", kv_get(conn, USER, "active_playlist"))
    print("last_recording    ", kv_get(conn, USER, "last_recording")["title"])
    print("preferences       ", kv_get(conn, USER, "session_preferences"))
    print("tool_calls        ", kv_get(conn, USER, "tool_calls"))
    print("missing key       ", kv_get(conn, USER, "nope", default="(default)"))

    # 4. TTL: a pending approval that expires after 1 second.
    kv_set(conn, USER, "pending_approval", "export pl-123 to spotify", ttl_s=1)
    print("\npending_approval  ", kv_get(conn, USER, "pending_approval"))
    time.sleep(1.1)
    print("...1 second later ", kv_get(conn, USER, "pending_approval"))

    # 5. Simulate a restart: close, reopen, read again.
    conn.close()
    conn = open_store()
    print("\nafter reopen      ", kv_get(conn, USER, "active_playlist")["title"])

    # Forget the user.
    print(f"\nforget {USER}: deleted {kv_forget_user(conn, USER)} keys")
    print("active_playlist   ", kv_get(conn, USER, "active_playlist"))

    conn.close()


#
# Redis alternative
#
# Same idea, but Redis is a separate server (great when
# several processes share state). With
#     pip install redis
#     docker run -p 6379:6379 redis
#
#     import json, redis
#     r = redis.Redis(decode_responses=True)
#
#     r.set("alice:active_playlist", json.dumps({...}))   # set
#     r.set("alice:pending_approval", "...", ex=60)       # set with TTL (s)
#     json.loads(r.get("alice:active_playlist"))           # get
#     r.delete("alice:active_playlist")                   # delete
#     for k in r.scan_iter("alice:*"):                    # forget user
#         r.delete(k)
#
# Redis expires keys for you; SQLite needs the
# expires_at column and check above.
#
