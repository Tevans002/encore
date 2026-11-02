# Hot State Guide (Working Memory Key-Value Store)

Part 5.1 asks for a small key-value store for **hot state**: the
high-frequency facts about the current session that the agent needs on
almost every step. This guide shows how to build it with SQLite (no
install, no server) and when Redis is worth it.

**Runnable example:** `ch07_06_sqlite_hot_store.py`

---

## 1. What belongs in hot state

| Key | Example value | Why it's hot |
| --- | --- | --- |
| `active_playlist` | `{"id": "…", "title": "[Encore] Focus — Oct 3", "track_mbids": [...]}` | Every "add that", "remove the second one" refers to it |
| `last_recording` | `{"mbid": "…", "title": "…", "artist": "…"}` | Resolves "that song", "love it" |
| `last_artist` | `{"mbid": "…", "name": "…"}` | Resolves "who's in that band" |
| `last_results` | `[{"n": 1, "mbid": "…"}, …]` | Resolves "the second song" |
| `pending_approval` | `{"tool": "export_playlist", "args": {...}}` | The loop must remember what it asked you to approve |
| `session_preferences` | `["no vocals", "no Christmas music"]` | Noted with `note_preference`; captured by the end-of-session summary |
| `tool_calls`, `step` | `7` | Budget and trace bookkeeping |

What does **not** belong here:

- Past sessions → episodic memory (Chroma).
- Documentation → semantic memory (Chroma).
- Your ListenBrainz history → read it with a tool; it's data, not memory.
- **Your token. Ever.**

## 2. Why SQLite

- Ships with Python (`import sqlite3`), so there's nothing to install.
- One file on disk (`memory/hot_state.db`), survives a crash mid-session.
- Real transactions, and `DELETE … WHERE user_id = ?` makes `--forget` one
  line.

A plain dict saved to a JSON file also meets the requirement. SQLite is
the step up when you want TTLs, safe concurrent writes, or many users.

## 3. The store

The table and four functions from `ch07_06`:

```python
import json, sqlite3, time

conn = sqlite3.connect("memory/hot_state.db")
conn.execute("""
    CREATE TABLE IF NOT EXISTS hot_state (
        user_id    TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT NOT NULL,     -- JSON
        expires_at REAL,              -- unix time, NULL = never
        PRIMARY KEY (user_id, key)
    )""")

def kv_set(conn, user_id, key, value, ttl_s=None): ...
def kv_get(conn, user_id, key, default=None): ...
def kv_delete(conn, user_id, key): ...
def kv_forget_user(conn, user_id) -> int: ...   # returns rows deleted
```

Key points:

- **Scope every key by `user_id`** (`ENCORE_USER_ID`). It's the deletion key
  for `--forget`.
- **Values are JSON**, so dicts and lists round-trip. Store only
  JSON-serializable data (MBIDs and names, not response objects).
- `INSERT OR REPLACE` makes `set` an overwrite.
- **Always use `?` placeholders**, never f-strings, in SQL.
- `conn.commit()` after writes, or they're lost if the process dies.

## 4. Using it in the loop

```python
# Session start: load hot state and put the key facts near the top of the context.
playlist = kv_get(conn, USER, "active_playlist")
last_rec = kv_get(conn, USER, "last_recording")
prefs    = kv_get(conn, USER, "session_preferences", [])

state_block = "\n".join(filter(None, [
    f"Active playlist: {playlist['title']} ({len(playlist['track_mbids'])} tracks)" if playlist else "",
    f"Last song discussed: {last_rec['title']} by {last_rec['artist']} ({last_rec['mbid']})" if last_rec else "",
    f"Preferences this session: {', '.join(prefs)}" if prefs else "",
]))

# After each tool call: update what the next step will need.
if tool == "search_recordings" and result.success:
    hits = result.data["recordings"]
    kv_set(conn, USER, "last_results", [{"n": i + 1, **h} for i, h in enumerate(hits)])
    kv_set(conn, USER, "last_recording", hits[0])

# Approval: remember what's waiting, clear it when answered.
kv_set(conn, USER, "pending_approval", {"tool": name, "args": args}, ttl_s=600)
...
kv_delete(conn, USER, "pending_approval")
```

The state block goes into the **front-loaded** part of the context
(Part 5.1: constraints, objective, key user facts, retrieved memories,
then conversation). It also survives working-memory compression, so a
summarized conversation still knows what "that one" means.

Because hot state lives outside the context window, it's also how you
satisfy "preserve the playlist being built and pending approvals" when you
compress old turns.

## 5. Session end and `--forget`

- At session end, the episodic summary should capture what matters from
  hot state (preferences, open threads). Then clear the session keys, or
  give them a TTL so stale state doesn't leak into the next session.
- `--forget` calls `kv_forget_user(conn, user_id)`, prints the count, and
  your test checks `SELECT COUNT(*) FROM hot_state WHERE user_id = ?` is 0.

## 6. Redis (optional)

Redis is an in-memory key-value server: use it when several processes share
state, or you want TTLs handled for you.

```bash
docker run -d --name redis -p 6379:6379 redis
pip install redis
```

```python
import json, redis
r = redis.Redis(decode_responses=True)

key = f"encore:{user_id}:active_playlist"          # namespace every key by user
r.set(key, json.dumps(playlist))                    # set
r.set(f"encore:{user_id}:pending_approval", json.dumps(p), ex=600)  # TTL in seconds
playlist = json.loads(r.get(key) or "null")         # get
for k in r.scan_iter(f"encore:{user_id}:*"):        # --forget
    r.delete(k)
```

It's another server to run and document, so for a single-user laptop
agent, SQLite is the simpler choice.

## 7. Common problems

| Symptom | Fix |
| --- | --- |
| Value didn't persist | Missing `conn.commit()`. |
| `ProgrammingError: SQLite objects created in a thread can only be used in that same thread` | Open the connection in the thread that uses it, or `sqlite3.connect(path, check_same_thread=False)` and don't share writes across threads. |
| `TypeError: Object of type … is not JSON serializable` | You stored a response object or a datetime. Store plain dicts/strings. |
| Old session's "that song" shows up in a new session | Clear session keys at session end, or give them a TTL. |
| `database is locked` | Another process holds a write. Keep transactions short; one writer at a time. |
