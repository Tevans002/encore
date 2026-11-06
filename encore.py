"""
Encore -- a ListenBrainz music agent that remembers.   (CSCI 391 midterm starter)

    python encore.py <online|offline>             # interactive session
    python encore.py <online|offline> --forget    # delete every memory for ENCORE_USER_ID

Copy chat() and model_for() from your Lab 3 agent and your router from Lab 4.
"""

import argparse
import json
import jsonschema
import os
import random
import sqlite3
from datetime import datetime, timezone
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional
from memory.hot_state import open_store, kv_get, kv_set, kv_delete, kv_forget_user
from memory.stores import open_client, open_collections

import requests
from dotenv import load_dotenv

#----------v_forget_user----------
from memory.stores import open_client, open_collections
#--------------------

load_dotenv()
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL_CHEAP", "cohere/north-mini-code:free")

LOCAL_LLM_URL = os.getenv("LOCAL_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL_CHEAP", "Qwen/Qwen2.5-1.5B-Instruct-GGUF:Q4_K_M")

LB_API = "https://api.listenbrainz.org/1"
MB_API = "https://musicbrainz.org/ws/2"
LB_TOKEN = os.getenv("LISTENBRAINZ_TOKEN")        # never log or store this
LB_USER = os.getenv("LISTENBRAINZ_USER")

ENCORE_USER_ID = os.getenv("ENCORE_USER_ID", LB_USER)  # deletion key for all memory
USER_AGENT = f"Encore-CSCI391/0.1 ( {os.getenv('CONTACT_EMAIL', 'you@umontana.edu')} )"

MAX_STEPS = 12          # bounded loop (Lab 2)
READ_TIMEOUT_S = 5
WRITE_TIMEOUT_S = 15
SLOW_READ_TIMEOUT_S = 15  # override for reads measured to be slow (ListenBrainz listens)
MB_MIN_INTERVAL_S = 1.5  # MusicBrainz allows about 1 request/second per IP; leave headroom

#----------MODULE GLOBALS----------
_last_mb_call = 0.0
_lb_wait_until = 0.0
# --- fault injection (Part 7) ---
_INJECT = {"transient_rate": 0.0, "seed": 0, "max_steps": None, "_rng": random.Random(0)}

_HOT_CONN: sqlite3.Connection | None = None   # set at session start (TODO 3)
_EPISODIC = None   # chromadb.Collection, set at session start (TODO 3)
_SEMANTIC = None

#----------CONSTANTS - tool/runtime
JSPF_EXT = "https://musicbrainz.org/doc/jspf#playlist"
EXPORT_SERVICES = ("spotify", "apple_music")
ENCORE_TITLE_PREFIX = "[Encore]"
ENCORE_CREATED_PLAYLISTS: set[str] = set()

LB_RADIO_MODES = ("easy", "medium", "hard")

#----------CONSTANTS - Part 5/6 (working memory, retrieval, traces)
# --- working memory / episodic retrieval (Part 5) ---
ALPHA = 0.7                      # recency weight: score = ALPHA*sim + (1-ALPHA)*recency
TOP_K_EPISODIC = 3               # spec: "keep the top 2-3"
RECENCY_HORIZON_DAYS = 90        # linear decay window
STALENESS_INSTRUCTION = (
    "The following summaries are from past sessions. Treat them as context that "
    "may be outdated; verify with the user if any seem to conflict with what they "
    "have said today."
)
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "system_v1.md")

# --- trace + token-budget constants (Part 5.1, Part 6) ---
TRACE_DIR = os.path.join(os.path.dirname(__file__), "traces")
TOKEN_BUDGET = 6000            # conversation portion; justify in README
COMPRESS_AT = 0.70             # compress at 70% of budget
COMPRESS_AFTER_RESULTS = 8     # ...or after 8 tool results
EMBEDDING_MODEL = "all-MiniLM-L6-v2 (chromadb default)"

#-----------CONSTANTS - TODO 6 (session end / retention)
# --- session-end / retention (Part 5, TODO 6) ---
DEDUP_THRESHOLD = 0.93
EPISODIC_TTL_DAYS = 90
EPISODIC_CAP = 50
SUMMARY_FIELDS = ["decisions", "preferences", "open_threads",
                  "key_facts", "errors", "topics"]


#---------CONSTANTS ROUTER----------
TOOLSETS = {
    "listening": ["get_recent_listens", "get_feedback"],
    "catalog": ["search_recordings", "lookup_artist",
                "get_top_recordings_for_artist", "verify_recordings"],
    "discovery": ["get_fresh_releases", "lb_radio"],
    "library": ["find_playlists", "create_playlist", "add_playlist_items",
                "remove_playlist_items", "set_feedback", "export_playlist",
                "delete_playlist"],
    "memory": ["search_past_sessions", "search_music_knowledge", "note_preference"],
    "control": ["final_answer"],
}
DOMAINS = list(TOOLSETS)   # auto-updates to the six Encore domains

ROUTER_PROMPT = """
You are a request router for Encore, a ListenBrainz music agent.
Call classify_intent with every toolset domain the user's request needs.
A request may need more than one.
Building a playlist from a genre, mood, or artist needs discovery (to find tracks), catalog (to verify recordings), and library (to create the playlist).

- listening: the user's own listening history, feedback, and tastes
- catalog: search and look up recordings and artists in MusicBrainz/ListenBrainz
- discovery: fresh releases and radio from a prompt or mood
- library: the user's playlists and feedback writes (find, create, add, remove, delete, export, love/hate)
- memory: recall past sessions, look up music knowledge, note a session preference
- control: end the turn with a final answer

Do not answer the request. Only classify it.
"""

CLASSIFIER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": "Report which toolset domains the request needs.",
        "parameters": {
            "type": "object",
            "properties": {
                "domains": {
                    "type": "array",
                    "items": {"type": "string", "enum": [
                        "listening",
                        "catalog",
                        "discovery",
                        "library",
                        "memory",
                        "control"
                        ]},
                    "description": "Every toolset domain the request needs."
                }
            },
            "required": ["domains"],
        },
    },
}
FORCE_CLASSIFIER = {"type": "function", "function": {"name": "classify_intent"}}

#----------CORE HELPERS----------
@dataclass
class ToolResult:                      # same contract as the Chapter 6 examples
    success: bool
    data: Optional[Any]
    error: Optional[str]
    error_category: Optional[str]      # "transient" | "input" | "resource" | "fatal"

    def to_json(self) -> str:
        return json.dumps(asdict(self))

def _track_ref(recording_mbid: str) -> dict:
    """JSPF track reference, the shape ListenBrainz playlist writes expect."""
    return {"identifier": f"https://musicbrainz.org/recording/{recording_mbid}"}

def ok(data: Any) -> ToolResult:
    return ToolResult(True, data, None, None)

def fail(category: str, message: str) -> ToolResult:
    return ToolResult(False, None, message, category)


def classify_status(status: int) -> str:
    if status == 0 or status == 429 or status >= 500:
        return "transient"
    if status in (401, 403):
        return "fatal"
    if status == 404:
        return "resource"
    return "input"

def http(method: str, url: str, *, params=None, body=None, auth=False, timeout=READ_TIMEOUT_S):
    """One polite request. Returns (status, json_or_text, headers). Status 0 = no response."""
    global _last_mb_call, _lb_wait_until, _INJECT
    # --- fault injection: synthetic transient failure before the network ---
    if _INJECT.get("transient_rate", 0.0) > 0 and (
            url.startswith(LB_API) or url.startswith(MB_API)):
        if _INJECT["_rng"].random() < _INJECT["transient_rate"]:
            status = _INJECT["_rng"].choice([429, 503])
            return status, f"injected {status}", {}

    if url.startswith(MB_API):
        wait = _last_mb_call + MB_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_mb_call = time.monotonic()
        params = {**(params or {}), "fmt": "json"}
    elif url.startswith(LB_API):
        wait = _lb_wait_until - time.monotonic()
        if wait > 0:                                  # honor the server's rate-limit headers
            time.sleep(wait)
    headers = {"User-Agent": USER_AGENT}
    if auth:
        headers["Authorization"] = f"Token {LB_TOKEN}"
    try:
        r = requests.request(method, url, params=params, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return 0, f"{type(exc).__name__}", {}
    if url.startswith(LB_API) and r.headers.get("X-RateLimit-Remaining") == "0":
        _lb_wait_until = time.monotonic() + float(r.headers.get("X-RateLimit-Reset-In", "1"))
    try:
        return r.status_code, r.json(), r.headers
    except ValueError:
        return r.status_code, r.text, r.headers

def backoff_delays(attempts: int = 4, base: float = 1.0, cap: float = 30.0):
    """Exponential backoff with full jitter. The LOOP retries transient errors, never the model."""
    return [random.uniform(0, min(cap, base * 2 ** i)) for i in range(attempts)]

# part 7
def load_inject(path: str) -> None:
    """Load --inject JSON: transient_rate, seed, max_steps."""
    global _INJECT
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    rate = float(cfg.get("transient_rate", 0.0))
    seed = int(cfg.get("seed", 0))
    _INJECT = {"transient_rate": max(0.0, min(1.0, rate)),
               "seed": seed,
               "max_steps": cfg.get("max_steps"),
               "_rng": random.Random(seed)}

#----------PROVIDED TOOLS----------
def get_recent_listens(count: int = 25) -> str:
    if not 1 <= count <= 100:
        return fail("input", f"count must be between 1 and 100; got {count}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/user/{LB_USER}/listens", params={"count": count},
                           timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status), f"ListenBrainz listens request failed (HTTP {status}).").to_json()
    listens = []
    for item in body["payload"]["listens"]:
        meta = item["track_metadata"]
        listens.append({
            "listened_at": item["listened_at"],
            "artist": meta.get("artist_name"),
            "track": meta.get("track_name"),
            "recording_mbid": (meta.get("mbid_mapping") or {}).get("recording_mbid"),
        })
    return ok({"listens": listens}).to_json()

def search_recordings(title: str, artist: str = "", limit: int = 5) -> str:
    if not title.strip():
        return fail("input", "title must not be empty.").to_json()
    query = f'recording:"{title}"' + (f' AND artist:"{artist}"' if artist else "")
    status, body, _ = http("GET", f"{MB_API}/recording", params={"query": query, "limit": limit})
    if status != 200:
        return fail(classify_status(status), f"MusicBrainz search failed (HTTP {status}).").to_json()
    # ORIGINAL PROVIDED STARTER CODE
    hits = [{
        "recording_mbid": r["id"],
        "title": r["title"],
        "artist": "".join(c["name"] + c.get("joinphrase", "") for c in r.get("artist-credit", [])),
        "length_s": (r.get("length") or 0) // 1000,
        "first_release": r.get("first-release-date"),
    } for r in body.get("recordings", [])]
    if not hits:
        return fail("resource", f"No MusicBrainz recording matches '{title}'"
                                f"{' by ' + artist if artist else ''}.").to_json()
    return ok({"recordings": hits}).to_json()

# ---------MY TOOLS - reads----------
# TODO 1: implement the remaining required tools in Part 3 (same ToolResult contract)
def get_feedback(score: int = 1, count: int = 25) -> str:
    """Loved (score=1) or hated (score=-1) recordings. [schema handles description]"""
    if score not in (1, -1):
        return fail("input", f"score must be 1 (loved) or -1 (hated); got {score}.").to_json()
    if not 1 <= count <= 100:
        return fail("input", f"count must be between 1 and 100; got {count}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/feedback/user/{LB_USER}/get-feedback",
                           params={"score": score, "count": count},
                           auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status), f"ListenBrainz feedback request failed (HTTP {status}).").to_json()
    feedback = [{
        "recording_mbid": fb.get("recording_mbid"),
        "recording_msid": fb.get("recording_msid"),
        "score": fb["score"],
    } for fb in body.get("feedback", [])]
    return ok({"score": score, "count": len(feedback), "feedback": feedback}).to_json()

def lookup_artist(artist_mbid: str) -> str:
    if not artist_mbid.strip():
        return fail("input", "artist_mbid must not be empty").to_json()

    status, body, _ = http("GET", f"{MB_API}/artist/{artist_mbid}",
                           params={"inc": "artist-rels"}, auth=False)

    if status != 200:
        return fail(classify_status(status), f"MusicBrainz artist lookup failed (HTTP {status}).").to_json()

    relations = []
    for rel in body.get("relations", []):
        relations.append({
            "type": rel.get("type"),
            "artist_mbid": rel["artist"]["id"],
            "artist_name": rel["artist"]["name"],
        })

    return ok({
        "artist_mbid": body["id"],
        "name": body["name"],
        "relations": relations,
    }).to_json()

def get_top_recordings_for_artist(artist_mbid: str, limit: int = 10) -> str:
    if not artist_mbid.strip():
        return fail("input", "artist_mbid must not be empty.").to_json()
    if not 1 <= limit <= 100:
        return fail("input", f"limit must be between 1 and 100; got {limit}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/popularity/top-recordings-for-artist/{artist_mbid}",
                           auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz popularity request failed (HTTP {status}).").to_json()
    top = [{
        "recording_mbid": r["recording_mbid"],
        "recording_name": r["recording_name"],
        "total_listen_count": r["total_listen_count"],
        "total_user_count": r["total_user_count"],
    } for r in body[:limit]]
    return ok({"artist_mbid": artist_mbid, "recordings": top}).to_json()

def get_fresh_releases(days: int = 30) -> str:
    if not 1 <= days <= 90:
        return fail("input", f"days must be between 1 and 90; got {days}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/user/{LB_USER}/fresh_releases",
                           params={"days": days}, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz fresh releases request failed (HTTP {status}).").to_json()
    releases = [{
        "release_mbid": rel["release_mbid"],
        "release_name": rel["release_name"],
        "artist_credit_name": rel["artist_credit_name"],
        "release_date": rel["release_date"],
    } for rel in body.get("payload", {}).get("releases", [])]
    return ok({"days": days, "releases": releases}).to_json()

def lb_radio(prompt: str, mode: str = "easy") -> str:
    if not prompt.strip():
        return fail("input", "prompt must not be empty.").to_json()
    if mode not in LB_RADIO_MODES:
        return fail("input", f"mode must be one of {LB_RADIO_MODES}; got {mode!r}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/explore/lb-radio",
                           params={"prompt": prompt, "mode": mode},
                           auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz radio request failed (HTTP {status}).").to_json()
    payload = body.get("payload", {})
    tracks = payload.get("jspf", {}).get("playlist", {}).get("track", [])
    out = []
    for t in tracks:
        ident = t["identifier"][0] if isinstance(t["identifier"], list) else t["identifier"]
        out.append({
            "recording_mbid": ident.rstrip("/").rsplit("/", 1)[-1],
            "title": t.get("title"),
            "creator": t.get("creator"),
        })
    return ok({"mode": mode, "tracks": out,
               "feedback": payload.get("feedback", [])}).to_json()

def find_playlists(count: int = 25) -> str:
    if not 1 <= count <= 100:
        return fail("input", f"count must be between 1 and 100; got {count}.").to_json()
    status, body, _ = http("GET", f"{LB_API}/user/{LB_USER}/playlists",
                           params={"count": count}, auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz playlist list request failed (HTTP {status}).").to_json()
    playlists = []
    for item in body.get("playlists", []):
        pl = item["playlist"]
        playlists.append({
            "playlist_mbid": pl["identifier"].rstrip("/").rsplit("/", 1)[-1],
            "title": pl["title"],
        })
    return ok({"playlist_count": body.get("playlist_count", len(playlists)),
               "playlists": playlists}).to_json()

def verify_recordings(recording_mbids: list[str]) -> str:
    if not recording_mbids:
        return fail("input", "recording_mbids must contain at least one MBID.").to_json()

    valid, invalid = [], []
    for mbid in recording_mbids:
        if not mbid.strip():
            invalid.append({"recording_mbid": mbid, "reason": "empty MBID"})
            continue
        status, body, _ = http("GET", f"{MB_API}/recording/{mbid}")
        if status == 200 and body.get("id") == mbid:
            valid.append({"recording_mbid": mbid, "title": body.get("title")})
        elif status == 404:
            invalid.append({"recording_mbid": mbid, "reason": "not found"})
        elif status == 400:
            invalid.append({"recording_mbid": mbid, "reason": "malformed MBID"})
        else:
            # A transient/fatal error on one MBID shouldn't invalidate the whole batch.
            return fail(classify_status(status),
                        f"MusicBrainz lookup failed for {mbid} (HTTP {status}).").to_json()

    return ok({"valid": valid, "invalid": invalid,
               "valid_count": len(valid), "invalid_count": len(invalid)}).to_json()

def search_past_sessions(query: str, n_results: int = 3) -> str:
    if not query.strip():
        return fail("input", "query must not be empty.").to_json()
    if not 1 <= n_results <= 10:
        return fail("input", f"n_results must be between 1 and 10; got {n_results}.").to_json()
    if _EPISODIC is None:
        return fail("fatal", "episodic memory is not initialized; session start must open_collections().").to_json()

    results = _EPISODIC.query(
        query_texts=[query],
        n_results=n_results,
        where={"user_id": ENCORE_USER_ID},
    )
    sessions = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        sessions.append({
            "summary": doc,
            "timestamp": meta.get("timestamp"),
            "topics": meta.get("topics"),
            "similarity": round(1 - dist, 3),
        })
    return ok({"query": query, "sessions": sessions}).to_json()

def search_music_knowledge(query: str, n_results: int = 3) -> str:
    if not query.strip():
        return fail("input", "query must not be empty.").to_json()
    if not 1 <= n_results <= 10:
        return fail("input", f"n_results must be between 1 and 10; got {n_results}.").to_json()
    if _SEMANTIC is None:
        return fail("fatal", "semantic memory is not initialized; session start must open_collections().").to_json()

    results = _SEMANTIC.query(query_texts=[query], n_results=n_results)
    facts = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        facts.append({
            "fact": doc,
            "source": meta.get("source"),
            "similarity": round(1 - dist, 3),
        })
    return ok({"query": query, "facts": facts}).to_json()

# -----------MY TOOLS WRITE DEFINITIONS---------
def _encore_playlists_with_title(title: str) -> list[dict]:
    """Return Encore playlists whose title matches (exact, after the marker)."""
    status, body, _ = http("GET", f"{LB_API}/user/{LB_USER}/playlists",
                           params={"count": 100}, auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return []
    out = []
    for item in body.get("playlists", []):
        pl = item["playlist"]
        t = pl.get("title", "")
        if t.startswith(ENCORE_TITLE_PREFIX):
            bare = t[len(ENCORE_TITLE_PREFIX):].strip()
            if bare == title.strip():
                out.append({"playlist_mbid": pl["identifier"].rstrip("/").rsplit("/", 1)[-1],
                            "title": t})
    return out

def create_playlist(title: str, recording_mbids: list[str]) -> str:
    if not title.strip():
        return fail("input", "title must not be empty.").to_json()
    if not recording_mbids:
        return fail("input", "recording_mbids must contain at least one MBID.").to_json()

    # Idempotent: if an Encore playlist with this title exists, add to it instead.
    existing = _encore_playlists_with_title(title)
    if existing:
        target = existing[0]["playlist_mbid"]
        add = add_playlist_items(target, recording_mbids)
        added = json.loads(add)
        if not added["success"]:
            return add
        return ok({"playlist_mbid": target, "title": f"{ENCORE_TITLE_PREFIX} {title}",
                   "track_count": None,
                   "note": "existing playlist updated, not duplicated"}).to_json()

    marked_title = f"{ENCORE_TITLE_PREFIX} {title.strip()}"
    body = {
        "playlist": {
            "title": marked_title,
            "track": [_track_ref(mbid) for mbid in recording_mbids],
            "extension": {JSPF_EXT: {"public": False}},
        }
    }
    status, resp, _ = http("POST", f"{LB_API}/playlist/create", body=body,
                           auth=True, timeout=WRITE_TIMEOUT_S)
    if status != 200 or resp.get("status") != "ok":
        return fail(classify_status(status),
                    f"ListenBrainz playlist create failed (HTTP {status}).").to_json()

    playlist_mbid = resp["playlist_mbid"]
    ENCORE_CREATED_PLAYLISTS.add(playlist_mbid)
    return ok({"playlist_mbid": playlist_mbid, "title": marked_title,
               "track_count": len(recording_mbids)}).to_json()

def add_playlist_items(playlist_mbid: str, recording_mbids: list[str]) -> str:
    if not playlist_mbid.strip():
        return fail("input", "playlist_mbid must not be empty.").to_json()
    if not recording_mbids:
        return fail("input", "recording_mbids must contain at least one MBID.").to_json()

    body = {"playlist": {"track": [_track_ref(mbid) for mbid in recording_mbids]}}
    status, resp, _ = http("POST", f"{LB_API}/playlist/{playlist_mbid}/item/add",
                           body=body, auth=True, timeout=WRITE_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz playlist add failed (HTTP {status}).").to_json()

    return ok({"playlist_mbid": playlist_mbid,
               "added_count": len(recording_mbids)}).to_json()

def remove_playlist_items(playlist_mbid: str, index: int, count: int = 1) -> str:
    if not playlist_mbid.strip():
        return fail("input", "playlist_mbid must not be empty.").to_json()
    if index < 0:
        return fail("input", f"index must be 0 or greater; got {index}.").to_json()
    if count < 1:
        return fail("input", f"count must be 1 or greater; got {count}.").to_json()

    body = {"index": index, "count": count}
    status, resp, _ = http("POST", f"{LB_API}/playlist/{playlist_mbid}/item/delete",
                           body=body, auth=True, timeout=WRITE_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz playlist remove failed (HTTP {status}).").to_json()

    return ok({"playlist_mbid": playlist_mbid,
               "removed_from_index": index,
               "removed_count": count}).to_json()

def delete_playlist(playlist_mbid: str) -> str:
    if not playlist_mbid.strip():
        return fail("input", "playlist_mbid must not be empty.").to_json()

    # Marker is the authority (survives restarts); the session set is a fast path.
    status, body, _ = http("GET", f"{LB_API}/playlist/{playlist_mbid}",
                           params={"fetch_metadata": "false"},
                           auth=True, timeout=SLOW_READ_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"Could not read playlist {playlist_mbid} (HTTP {status}).").to_json()
    title = body.get("playlist", {}).get("title", "")
    if not title.startswith(ENCORE_TITLE_PREFIX):
        return fail("input",
                    f"playlist {playlist_mbid} does not carry Encore's '{ENCORE_TITLE_PREFIX}' "
                    "marker; refusing to delete it.").to_json()

    status, resp, _ = http("POST", f"{LB_API}/playlist/{playlist_mbid}/delete",
                           auth=True, timeout=WRITE_TIMEOUT_S)
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz playlist delete failed (HTTP {status}).").to_json()

    ENCORE_CREATED_PLAYLISTS.discard(playlist_mbid)
    return ok({"playlist_mbid": playlist_mbid, "deleted": True}).to_json()

def set_feedback(recording_mbid: str, score: int) -> str:
    if not recording_mbid.strip():
        return fail("input", "recording_mbid must not be empty.").to_json()
    if score not in (1, 0, -1):
        return fail("input", f"score must be 1 (love), 0 (clear), or -1 (hate); got {score}.").to_json()

    body = {"recording_mbid": recording_mbid, "score": score}
    status, resp, _ = http("POST", f"{LB_API}/feedback/recording-feedback",
                           body=body, auth=True, timeout=WRITE_TIMEOUT_S)
    if status != 200 or resp.get("status") != "ok":
        return fail(classify_status(status),
                    f"ListenBrainz feedback update failed (HTTP {status}).").to_json()

    return ok({"recording_mbid": recording_mbid, "score": score}).to_json()

def export_playlist(playlist_mbid: str, service: str) -> str:
    if not playlist_mbid.strip():
        return fail("input", "playlist_mbid must not be empty.").to_json()
    if service not in EXPORT_SERVICES:
        return fail("input", f"service must be one of {EXPORT_SERVICES}; got {service!r}.").to_json()

    status, resp, _ = http("POST", f"{LB_API}/playlist/{playlist_mbid}/export/{service}",
                           auth=True, timeout=WRITE_TIMEOUT_S)
    if status in (401, 403):
        return fail("fatal",
                    f"Export to {service} failed (HTTP {status}): the service is not connected "
                    "in ListenBrainz settings. Connect it with playlist permission; do not retry."
                    ).to_json()
    if status != 200:
        return fail(classify_status(status),
                    f"ListenBrainz playlist export to {service} failed (HTTP {status}).").to_json()

    return ok({"playlist_mbid": playlist_mbid, "service": service,
               "external_playlist_url": resp.get("external_url") if isinstance(resp, dict) else None,
               "note": "Created in the external streaming account; Encore cannot undo this."}).to_json()

def note_preference(preference: str) -> str:
    if not preference.strip():
        return fail("input", "preference must not be empty.").to_json()
    if _HOT_CONN is None:
        return fail("fatal", "hot state is not initialized; session start must call open_store().").to_json()

    prefs = kv_get(_HOT_CONN, ENCORE_USER_ID, "session_preferences", default=[]) or []
    if preference in prefs:
        return ok({"preference": preference, "preferences": prefs,
                   "note": "already recorded; unchanged"}).to_json()
    prefs.append(preference)
    kv_set(_HOT_CONN, ENCORE_USER_ID, "session_preferences", prefs)
    return ok({"preference": preference, "preferences": prefs}).to_json()

#------------MY TOOLS final_answer sentinel - control----------
def final_answer(status: str, answer: str, confidence: str, sources: list[str]) -> str:
    """Sentinel: the loop terminates on this call. Returns the validated payload."""
    return ok({"status": status, "answer": answer,
               "confidence": confidence, "sources": sources}).to_json()

#----------MODEL FUNCTIONS----------
def model_for(tier: str, mode: str) -> str:
    """Model string for a tier ('cheap'|'expensive') on a backend ('online'|'offline')."""
    if mode == "online":
        if tier == "expensive":
            return os.getenv("OPENROUTER_MODEL_EXPENSIVE", OPENROUTER_MODEL)
        return os.getenv("OPENROUTER_MODEL_CHEAP", OPENROUTER_MODEL)
    if tier == "expensive":
        return os.getenv("LOCAL_LLM_MODEL_EXPENSIVE", LOCAL_LLM_MODEL)
    return os.getenv("LOCAL_LLM_MODEL_CHEAP", LOCAL_LLM_MODEL)

def route(prompt, mode):
    if mode == "online":
        url, model = OPENROUTER_URL, OPENROUTER_MODEL
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    else:
        url, model = LOCAL_LLM_URL, LOCAL_LLM_MODEL
        headers = {}

    response = requests.post(
        url, headers=headers,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "tools": [CLASSIFIER_SCHEMA],
            "tool_choice": FORCE_CLASSIFIER,
            "temperature": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]

def chat(messages, mode, tools=None, tool_choice=None, model=None):
    """General chat for the ReAct loop. messages = list of {role, content, ...}."""
    if mode == "online":
        url = OPENROUTER_URL
        default_model = OPENROUTER_MODEL
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    else:
        url = LOCAL_LLM_URL
        default_model = LOCAL_LLM_MODEL
        headers = {}

    payload = {"model": model or default_model, "messages": messages, "temperature": 0}
    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    response = requests.post(url, headers=headers, json=payload, timeout=60)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]

#---------ROUTER FUNCTION----------
def get_available_tools(prompt, mode):
    message = route(prompt, mode)      # ← FIX: was chat(prompt, mode)

    fallback = list(DOMAINS)
    domains = fallback

    try:
        raw = message["tool_calls"][0]["function"]["arguments"]
        args = json.loads(raw)
        jsonschema.validate(args, CLASSIFIER_SCHEMA["function"]["parameters"])
        domains = args["domains"]
        if not domains:
            domains = fallback
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, jsonschema.ValidationError) as e:
        print(f"[fallback] {type(e).__name__}: {e}")
        domains = fallback

    if "control" not in domains:
        domains = domains + ["control"]   # always offer final_answer
    tools = []
    for domain in domains:
        tools.extend(TOOLSETS.get(domain, []))
    return {"domains": domains, "tools": tools}

# TODO 2: write a JSON schema for every tool: descriptions say when NOT to use it,
#         bounded values are enums, write tools state their side effects (Part 3)

# ----------READ TOOL SCHEMAS----------
GET_RECENT_LISTENS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_recent_listens",
        "description": (
            "Return the user's most recent listens from ListenBrainz, newest first. "
            "Pass count (1-100) to control how many. "
            "Prefer this to learn what the user has actually been playing lately. "
            "Do NOT use this to fetch an artist's catalog — use get_top_recordings_for_artist. "
            "Do NOT use this to see loved/hated tracks — use get_feedback. "
            "Do NOT use this for new releases — use get_fresh_releases. "
            "Read-only, no side effects. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25,
                    "description": "How many recent listens to return (1-100).",
                }
            },
            "required": [],
        },
    },
}

SEARCH_RECORDINGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_recordings",
        "description": (
            "Search MusicBrainz for recordings by title, optionally narrowed by artist. "
            "Pass a title and (optionally) an artist name; returns up to limit matches "
            "with recording MBIDs, titles, artist names, lengths, and first release dates. "
            "Prefer this when you have a title and need the recording MBID before writing a playlist. "
            "Do NOT use this to look up an artist's relationships — use lookup_artist. "
            "Do NOT use this to browse an artist's popular tracks — use get_top_recordings_for_artist. "
            "Do NOT use this with an empty title; at least a title is required. "
            "Read-only, no side effects. "
            "If you need to confirm MBIDs from another source, call verify_recordings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Recording title to search for (required, non-empty).",
                },
                "artist": {
                    "type": "string",
                    "default": "",
                    "description": "Optional artist name to narrow the search.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "default": 5,
                    "description": "Maximum number of matches to return (1-25).",
                },
            },
            "required": ["title"],
        },
    },
}

GET_FEEDBACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_feedback",
        "description": (
            "List recordings you have loved (score=1) or hated (score=-1) on ListenBrainz. "
            "Use this to learn the user's taste before recommending or building a playlist. "
            "Prefer this over search_recordings when you want the user's OWN opinions, not catalog data. "
            "Do NOT use this to change feedback — call set_feedback for that. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "score": {
                    "type": "integer",
                    "enum": [1, -1],
                    "description": "1 = loved recordings; -1 = hated recordings."
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25,
                    "description": "How many to return (1-100)."
                }
            },
            "required": []
        }
    }
}

LOOKUP_ARTIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lookup_artist",
        "description": (
            "Look up a MusicBrainz artist by MBID and return the artist's id, name, "
            "and relationships (band members, collaborators). "
            "Pass the artist's MusicBrainz ID (MBID). "
            "Prefer this when you need artist relationships rather than recording search results. "
            "Do NOT use this to find recordings — use search_recordings. "
            "Do NOT use this to find an artist by name — this tool requires an MBID "
            "and cannot search by name. "
            "Do NOT use this when you only have an artist name — no available tool "
            "converts an artist name to an artist MBID, so this tool is not appropriate "
            "in that case."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist_mbid": {
                    "type": "string",
                    "description": (
                        "MusicBrainz artist ID (UUID). "
                        "You must already have the artist MBID — the available tools do not "
                        "provide an artist-name-to-MBID search. If you only have a name, "
                        "this tool is not appropriate; use search_recordings for "
                        "recording-level results instead."
                    ),
                }
            },
            "required": ["artist_mbid"],
        },
    },
}

GET_TOP_RECORDINGS_FOR_ARTIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_top_recordings_for_artist",
        "description": (
            "Return an artist's most-listened recordings on ListenBrainz, most popular first. "
            "Pass the artist's MusicBrainz ID (MBID). "
            "Prefer this over search_recordings when you want the artist's POPULAR catalog "
            "rather than a title match. "
            "Do NOT use this to search by recording title — use search_recordings. "
            "Do NOT use this without an artist MBID; no tool resolves an artist name to an MBID. "
            "Do NOT use this to learn the user's own taste — use get_feedback for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "artist_mbid": {
                    "type": "string",
                    "description": "MusicBrainz artist ID (UUID). Must already be known.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                    "description": "How many top recordings to return (1-100).",
                },
            },
            "required": ["artist_mbid"],
        },
    },
}

GET_FRESH_RELEASES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_fresh_releases",
        "description": (
            "List new releases from artists the user listens to, within the last N days. "
            "Pass days (1-90). "
            "Prefer this for 'what's new from my artists' style requests. "
            "Do NOT use this to explore a specific artist's catalog — use "
            "get_top_recordings_for_artist or lookup_artist. "
            "Do NOT use this to find the user's own listening history — use get_recent_listens."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 90,
                    "default": 30,
                    "description": "Look-back window in days (1-90).",
                }
            },
            "required": [],
        },
    },
}

LB_RADIO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lb_radio",
        "description": (
            "Generate a ListenBrainz radio playlist from a free-text prompt "
            "(e.g. 'artist:(Pearl Jam)' or, for a genre, 'tag:(metal)'; 'genre:' is not valid), in easy, medium, or hard mode. "
            "Prefer this to discover music from a mood or seed rather than a known list. "
            "Do NOT use this when you already have specific recording MBIDs to play — "
            "use create_playlist instead. "
            "Do NOT use this to look up an artist's catalog — use get_top_recordings_for_artist. "
            "Radio mode must be one of the enum values; an empty prompt is rejected."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "ListenBrainz radio prompt, e.g. 'artist:(Pearl Jam)'.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["easy", "medium", "hard"],
                    "default": "easy",
                    "description": "Radio difficulty: easy, medium, or hard.",
                },
            },
            "required": ["prompt"],
        },
    },
}

FIND_PLAYLISTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_playlists",
        "description": (
            "List the user's own ListenBrainz playlists, including private ones, "
            "with MBID and title only. "
            "Prefer this before create_playlist to avoid duplicates, and before "
            "add_playlist_items / remove_playlist_items to find the target playlist MBID. "
            "Do NOT use this to open a playlist's tracks — this returns metadata only. "
            "Do NOT use this to search public playlists from other users."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25,
                    "description": "How many playlists to list (1-100).",
                }
            },
            "required": [],
        },
    },
}

VERIFY_RECORDINGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "verify_recordings",
        "description": (
            "Confirm that recording MBIDs exist in MusicBrainz before writing a playlist. "
            "Pass a list of recording MBIDs; returns which are valid (with titles) and which are not. "
            "Prefer this before any create_playlist or add_playlist_items call when MBIDs "
            "may be uncertain or were gathered from outside the toolset. "
            "Do NOT use this to search by title — use search_recordings. "
            "Do NOT use this to write anything — it is read-only. "
            "Do NOT use this after a successful write — verify before, not after. "
            "Read-only, no side effects. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recording_mbids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Recording MBIDs to verify. "
                        "If you only have titles, call search_recordings first to get MBIDs."
                    ),
                },
            },
            "required": ["recording_mbids"],
        },
    },
}

SEARCH_PAST_SESSIONS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_past_sessions",
        "description": (
            "Search the user's past-session summaries by meaning, returning the most similar "
            "sessions with their timestamps and topics. "
            "Pass a natural-language query and an optional result count. "
            "Prefer this at the start of a request to recall what the user did or preferred before. "
            "Do NOT use this to search general music facts — use search_music_knowledge. "
            "Do NOT use this to read the current session's state — that lives in hot state. "
            "Read-only, no side effects."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to recall, in natural language.",
                },
                "n_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                    "description": "How many past sessions to return (1-10).",
                },
            },
            "required": ["query"],
        },
    },
}

SEARCH_MUSIC_KNOWLEDGE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_music_knowledge",
        "description": (
            "Search shared music knowledge (glossary, ListenBrainz/MusicBrainz docs) by meaning. "
            "Pass a natural-language query and an optional result count. "
            "Prefer this to answer 'what does X mean' or 'how does Y work' questions. "
            "Do NOT use this to recall the user's own past sessions — use search_past_sessions. "
            "Do NOT use this to read current session state — that lives in hot state. "
            "Read-only, no side effects."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The music concept or question to look up.",
                },
                "n_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 3,
                    "description": "How many knowledge chunks to return (1-10).",
                },
            },
            "required": ["query"],
        },
    },
}
# ----------WRITE TOOL SCHEMAS----------
CREATE_PLAYLIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_playlist",
        "description": (
            "Create a new PRIVATE ListenBrainz playlist and return its MBID. "
            "Pass a title and an ordered list of recording MBIDs. "
            "Prefer this when the user asks you to build or save a playlist. "
            "Do NOT use this to add tracks to an existing playlist — use add_playlist_items. "
            "Do NOT use this with unverified MBIDs — call verify_recordings first. "
            "Do NOT use this to duplicate an existing playlist — call find_playlists first. "
            "SIDE EFFECT: creates a real playlist on the user's account. "
            "NOT IDEMPOTENT: calling twice with the same title creates two playlists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Playlist title shown in ListenBrainz.",
                },
                "recording_mbids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Ordered recording MBIDs. Must already be verified — "
                        "call verify_recordings first if you only have titles or MSIDs."
                    ),
                },
            },
            "required": ["title", "recording_mbids"],
        },
    },
}

ADD_PLAYLIST_ITEMS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "add_playlist_items",
        "description": (
            "Append recordings to the end of an existing ListenBrainz playlist. "
            "Pass the playlist MBID and an ordered list of recording MBIDs to append. "
            "Prefer this when adding tracks to a playlist that already exists. "
            "Do NOT use this to create a new playlist — use create_playlist. "
            "Do NOT use this to remove tracks — use remove_playlist_items. "
            "Do NOT use this with unverified MBIDs — call verify_recordings first. "
            "SIDE EFFECT: modifies the playlist permanently. "
            "NOT IDEMPOTENT: adding the same recording twice appends it twice. "
            "Tracks are appended at the END; existing order is unchanged. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "playlist_mbid": {
                    "type": "string",
                    "description": (
                        "MBID of an existing playlist. "
                        "If unknown, call find_playlists first."
                    ),
                },
                "recording_mbids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Ordered recording MBIDs to append. Must already be verified — "
                        "call verify_recordings first if you only have titles or MSIDs."
                    ),
                },
            },
            "required": ["playlist_mbid", "recording_mbids"],
        },
    },
}

REMOVE_PLAYLIST_ITEMS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "remove_playlist_items",
        "description": (
            "Remove tracks from a ListenBrainz playlist by POSITION (index and count), "
            "not by recording MBID. "
            "Pass the playlist MBID, a 0-based index, and how many items to remove. "
            "Prefer this when the user wants specific tracks removed. "
            "Do NOT use this to add tracks — use add_playlist_items. "
            "Do NOT use this to delete the whole playlist — use delete_playlist. "
            "Do NOT use this by recording MBID — this tool is position-based; "
            "to find a track's position, the playlist's track list must be read first. "
            "SIDE EFFECT: permanently removes tracks from the playlist. "
            "NOT IDEMPOTENT: removing index 0 twice removes two different tracks, "
            "because removing a track shifts every later track's index down by one. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "playlist_mbid": {
                    "type": "string",
                    "description": (
                        "MBID of an existing playlist. "
                        "If unknown, call find_playlists first."
                    ),
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "0-based position of the first track to remove. "
                        "Track positions shift after each removal."
                    ),
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "How many consecutive tracks to remove starting at index.",
                },
            },
            "required": ["playlist_mbid", "index"],
        },
    },
}

SET_FEEDBACK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_feedback",
        "description": (
            "Set your feedback on a single recording: love (1), hate (-1), or clear (0). "
            "Pass the recording MBID and an integer score. "
            "Prefer this when the user says they love, hate, or want to clear their "
            "opinion on a specific track. "
            "Do NOT use this to read existing feedback — use get_feedback. "
            "Do NOT use this to set feedback on many recordings at once — call it once per recording. "
            "SIDE EFFECT: changes the user's stored feedback on ListenBrainz. "
            "IDEMPOTENT: setting the same score twice has the same result as once. "
            "A score outside -1, 0, 1 is rejected as an input error. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recording_mbid": {
                    "type": "string",
                    "description": (
                        "MusicBrainz recording ID (UUID) to give feedback on. "
                        "If you don't have it, call search_recordings first."
                    ),
                },
                "score": {
                    "type": "integer",
                    "enum": [1, 0, -1],
                    "description": "1 = love, 0 = clear/no feedback, -1 = hate.",
                },
            },
            "required": ["recording_mbid", "score"],
        },
    },
}

DELETE_PLAYLIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_playlist",
        "description": (
            "Delete a playlist that Encore created earlier in this session. "
            "Pass the playlist MBID. "
            "Prefer this only to undo or clean up a playlist Encore itself created. "
            "Do NOT use this to delete a playlist Encore did not create — "
            "only Encore-created playlists are eligible; the call is refused otherwise. "
            "Do NOT use this to remove individual tracks — use remove_playlist_items. "
            "Do NOT use this to delete a playlist you only suspect is unwanted — confirm with the user first. "
            "SIDE EFFECT: permanently deletes the playlist from the user's account. "
            "IDEMPOTENT: a second delete of the same MBID fails with a resource error "
            "(the playlist is already gone). "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "playlist_mbid": {
                    "type": "string",
                    "description": (
                        "MBID of an Encore-created playlist. "
                        "If you don't have it, call find_playlists first — "
                        "but only Encore-created playlists are eligible."
                    ),
                },
            },
            "required": ["playlist_mbid"],
        },
    },
}

EXPORT_PLAYLIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "export_playlist",
        "description": (
            "Export a ListenBrainz playlist to Spotify or Apple Music. "
            "Pass the playlist MBID and the target service. "
            "Prefer this only after the user has connected the target service in "
            "ListenBrainz settings with playlist permission. "
            "Do NOT use this if the service is not connected — the call fails with a "
            "fatal error that tells the user to connect the service; do not retry it. "
            "Do NOT use this to create a ListenBrainz playlist — use create_playlist. "
            "Do NOT use this to export to a service other than Spotify or Apple Music. "
            "SIDE EFFECT: creates a real playlist in the user's external streaming account. "
            "NOT IDEMPOTENT: exporting the same playlist twice creates two external playlists. "
            "This action cannot be undone by Encore and must be verified manually. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "playlist_mbid": {
                    "type": "string",
                    "description": (
                        "MBID of the ListenBrainz playlist to export. "
                        "If unknown, call find_playlists first."
                    ),
                },
                "service": {
                    "type": "string",
                    "enum": ["spotify", "apple_music"],
                    "description": "Target streaming service: spotify or apple_music.",
                },
            },
            "required": ["playlist_mbid", "service"],
        },
    },
}

NOTE_PREFERENCE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "note_preference",
        "description": (
            "Record a user preference for this session (e.g. 'no vocals', 'no Christmas music'). "
            "Pass a short preference string. "
            "Prefer this when the user states a like or dislike that should shape future "
            "recommendations in this session. "
            "Do NOT use this for feedback on a specific recording — use set_feedback. "
            "Do NOT use this for facts about the user's long-term taste — those belong in memory, "
            "not hot state. "
            "SIDE EFFECT: writes to session hot state; cleared or summarized at session end. "
            "IDEMPOTENT: noting the same preference twice leaves the list unchanged. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "preference": {
                    "type": "string",
                    "description": "A short preference to remember for this session.",
                },
            },
            "required": ["preference"],
        },
    },
}

FINAL_ANSWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "final_answer",
        "description": (
            "End the turn with the user-facing answer. Call this exactly once, when you "
            "have finished acting. Pass the status, the answer text, your confidence, "
            "and the sources you used. "
            "Do NOT call this mid-task; finish the tool work first. "
            "Do NOT call any tool after this. "
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["answered", "answered_low_confidence", "stuck",
                             "budget_exceeded", "policy_halt"],
                    "description": "How the request ended.",
                },
                "answer": {"type": "string", "description": "The user-facing answer."},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "Confidence in the answer.",
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recording MBIDs, playlist MBIDs, or knowledge chunks used.",
                },
            },
            "required": ["status", "answer", "confidence", "sources"],
        },
    },
}

TOOL_SCHEMAS = [
    GET_RECENT_LISTENS_SCHEMA,
    SEARCH_RECORDINGS_SCHEMA,
    GET_FEEDBACK_SCHEMA,
    LOOKUP_ARTIST_SCHEMA,
    GET_TOP_RECORDINGS_FOR_ARTIST_SCHEMA,
    GET_FRESH_RELEASES_SCHEMA,
    LB_RADIO_SCHEMA,
    FIND_PLAYLISTS_SCHEMA,
    VERIFY_RECORDINGS_SCHEMA,
    SEARCH_PAST_SESSIONS_SCHEMA,
    SEARCH_MUSIC_KNOWLEDGE_SCHEMA,
    # writes
    CREATE_PLAYLIST_SCHEMA,
    ADD_PLAYLIST_ITEMS_SCHEMA,
    REMOVE_PLAYLIST_ITEMS_SCHEMA,
    DELETE_PLAYLIST_SCHEMA,
    SET_FEEDBACK_SCHEMA,
    EXPORT_PLAYLIST_SCHEMA,
    NOTE_PREFERENCE_SCHEMA,
    FINAL_ANSWER_SCHEMA,
]

TOOLS = {
    # reads
    "get_recent_listens": get_recent_listens,
    "search_recordings": search_recordings,
    "get_feedback": get_feedback,
    "lookup_artist": lookup_artist,
    "get_top_recordings_for_artist": get_top_recordings_for_artist,
    "get_fresh_releases": get_fresh_releases,
    "lb_radio": lb_radio,
    "find_playlists": find_playlists,
    "verify_recordings": verify_recordings,
    "search_past_sessions": search_past_sessions,
    "search_music_knowledge": search_music_knowledge,
    # writes
    "create_playlist": create_playlist,
    "add_playlist_items": add_playlist_items,
    "remove_playlist_items": remove_playlist_items,
    "delete_playlist": delete_playlist,
    "set_feedback": set_feedback,
    "export_playlist": export_playlist,
    "note_preference": note_preference,
    #control
    "final_answer": final_answer,
}
WRITE_TOOLS = {
    "create_playlist",
    "add_playlist_items",
    "remove_playlist_items",
    "delete_playlist",
    "set_feedback",
    "export_playlist",
    "note_preference",
}

#----------MEMORY HELPERS - Part 5 (sessioin start / retrieval)----------
def load_procedural() -> str:
        """Read the versioned system prompt. Returns the full text, or '' if missing."""
        try:
            with open(PROMPT_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

def _extract_rules_block(procedural: str) -> str:
    """Return the text under '# Rules' up to the next '# ' heading."""
    if not procedural:
        return ""
    lines = procedural.splitlines()
    out, capturing = [], False
    for line in lines:
        if line.startswith("# Rules"):
            capturing = True
            continue
        if capturing and line.startswith("# "):
            break
        if capturing:
            out.append(line)
    return "\n".join(out).strip()

def _recency_score(timestamp: str) -> float:
    """Linear decay over RECENCY_HORIZON_DAYS: 1.0 today, 0.0 at the horizon."""
    try:
        then = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    age_days = (datetime.now(timezone.utc) - then).total_seconds() / 86400.0
    return max(0.0, 1.0 - age_days / RECENCY_HORIZON_DAYS)

def retrieve_episodic(first_message: str) -> dict:
    """Query episodic memory for the first message; re-rank by similarity+recency; top K.

    Returns {"summaries": [...], "key_facts": [...]}. Empty (not error) if no memories.
    """
    empty = {"summaries": [], "key_facts": []}
    if _EPISODIC is None:
        return empty
    if not first_message.strip():
        return empty

    results = _EPISODIC.query(
        query_texts=[first_message],
        n_results=max(TOP_K_EPISODIC * 3, 6),   # over-fetch, then re-rank
        where={"user_id": ENCORE_USER_ID},
    )
    docs = results["documents"][0]
    metas = results["metadatas"][0]
    dists = results["distances"][0]
    if not docs:
        return empty

    ranked = []
    for doc, meta, dist in zip(docs, metas, dists):
        similarity = 1 - dist
        recency = _recency_score(meta.get("timestamp", ""))
        score = ALPHA * similarity + (1 - ALPHA) * recency
        ranked.append({
            "summary": doc,
            "timestamp": meta.get("timestamp"),
            "topics": meta.get("topics"),
            "quality": meta.get("quality", "ok"),
            "similarity": round(similarity, 3),
            "score": round(score, 3),
            "key_facts": meta.get("key_facts", []),
        })
    ranked.sort(key=lambda r: r["score"], reverse=True)
    top = ranked[:TOP_K_EPISODIC]

    # Aggregate key user facts from the top summaries, deduped, order preserved.
    seen, facts = set(), []
    for r in top:
        raw = r.get("key_facts")
        if isinstance(raw, str):          # Chroma stores lists as CSV strings
            raw = [x.strip() for x in raw.split(",") if x.strip()]
        for fact in (raw or []):
            if fact and fact not in seen:
                seen.add(fact)
                facts.append(fact)

    return {"summaries": top, "key_facts": facts}

def build_state_block() -> str:
    """Current-session facts from hot state (Part 5.1: 'key facts about the user')."""
    if _HOT_CONN is None:
        return ""
    playlist = kv_get(_HOT_CONN, ENCORE_USER_ID, "active_playlist")
    last_rec = kv_get(_HOT_CONN, ENCORE_USER_ID, "last_recording")
    last_art = kv_get(_HOT_CONN, ENCORE_USER_ID, "last_artist")
    prefs = kv_get(_HOT_CONN, ENCORE_USER_ID, "session_preferences", []) or []

    lines = []
    if playlist:
        lines.append(f"Active playlist: {playlist.get('title')} "
                    f"({len(playlist.get('track_mbids', []))} tracks)")
    if last_rec:
        lines.append(f"Last song discussed: {last_rec.get('title')} by "
                    f"{last_rec.get('artist')} ({last_rec.get('mbid')})")
    if last_art:
        lines.append(f"Last artist discussed: {last_art.get('name')} ({last_art.get('mbid')})")
    if prefs:
        lines.append(f"Preferences this session: {', '.join(prefs)}")
    return "\n".join(lines)

def build_front_loaded_context(rules: str, objective: str, state_block: str,
                            key_facts: list, summaries: list) -> str:
    """Assemble the front-loaded context in Part 5.1 order."""
    parts = []

    # 1. Reinjected constraints (the Rules block from procedural memory)
    if rules:
        parts.append(f"## Rules (always apply)\n{rules}")

    # 2. Current objective
    parts.append(f"## Current request\n{objective}")

    # 3. Key facts about the user (hot state + episodic key_facts)
    fact_lines = []
    if state_block:
        fact_lines.append(state_block)
    if key_facts:
        fact_lines.append("From past sessions:\n" + "\n".join(f"- {f}" for f in key_facts))
    if fact_lines:
        parts.append("## Key facts about the user\n" + "\n".join(fact_lines))

    # 4. Retrieved memories (with dates + staleness instruction)
    if summaries:
        mem_lines = [STALENESS_INSTRUCTION, ""]
        for s in summaries:
            date = (s.get("timestamp") or "")[:10]
            flag = "" if s.get("quality", "ok") == "ok" else "  [low-confidence: verify]"
            mem_lines.append(f"- ({date}) {s['summary']}{flag}")
        parts.append("## Relevant past sessions\n" + "\n".join(mem_lines))

    return "\n\n".join(parts)

#----------MEMORY HELPERS - TODO 6 (session end / retention)
def _summarize_session(messages: list, mode: str) -> dict:
    prompt = (
        "Summarize this session as JSON with exactly these keys, each a list of short strings. "
        "If the session only looked something up, record WHAT was looked up under key_facts "
        "(e.g. 'User looked up The River by Parkway Drive'). "
        "decisions (what was created or changed), preferences (likes/dislikes stated), "
        "open_threads (unfinished requests), key_facts (anything worth remembering next session), "
        "errors (mistakes to avoid), topics (2-4 keywords). "
        "key_facts are durable facts about the USER or their music (e.g. 'Prefers instrumental tracks'), "
        "NOT the assistant's replies, plans, or questions. Never store the assistant's own words. "
        "Return ONLY the JSON object, with no prose before or after.\n\n"
        "TRANSCRIPT:\n" + "\n".join(
            str(m.get("content")) for m in messages
            if m.get("content")
            and "final_answer(" not in str(m.get("content"))
            and "tool_call" not in str(m.get("content"))
        )
    )
    msg = chat([{"role": "user", "content": prompt}], mode,
               model=model_for("cheap", mode))
    raw = msg.get("content") or ""
    #print(f"[summarize raw] {raw[:400]!r}") <- DEBUG
    try:
        start, end = raw.find("{"), raw.rfind("}") + 1
        data = json.loads(raw[start:end])
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, json.JSONDecodeError):
        # Fallback: a non-JSON reply still carries information. Keep it as one key_fact.
        text = raw.strip()[:500]
        data = {"key_facts": [text]} if text else {}
    return {f: ([x for x in data.get(f, []) if isinstance(x, str)]
                if isinstance(data.get(f, []), list) else []) for f in SUMMARY_FIELDS}


def _quality_check(summary: dict, messages: list, mode: str) -> tuple[str, str]:
    """Second cheap-model call scores factual consistency (Part 5). Returns (flag, note)."""
    transcript = "\n".join(str(m.get("content")) for m in messages if m.get("content"))
    prompt = (
        "Score the summary for factual consistency with the transcript. "
        "Reply with one word: OK or FLAG, then a short reason. "
        f"SUMMARY: {json.dumps(summary)}\nTRANSCRIPT: {transcript}"
    )
    msg = chat([{"role": "user", "content": prompt}], mode,
               model=model_for("cheap", mode))
    text = (msg.get("content") or "").strip()
    flag = "FLAG" if text.upper().startswith("FLAG") else "OK"
    return flag, text[:200]


def _dedup_and_store(summary: dict, quality_flag: str, mode: str) -> str:
    """Embed, find near-duplicate same-user summary, merge or upsert (Part 5)."""
    doc = json.dumps(summary)
    meta = {"user_id": ENCORE_USER_ID,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "topics": ",".join(summary.get("topics", [])),
            "quality": "ok" if quality_flag == "OK" else "flagged",
            "embedding_model": EMBEDDING_MODEL,
            "key_facts": ",".join(summary.get("key_facts", []))}

    # dedup: query same user, check cosine >= threshold
    existing = _EPISODIC.query(query_texts=[doc], n_results=1,
                               where={"user_id": ENCORE_USER_ID})
    if existing["documents"][0]:
        sim = 1 - existing["distances"][0][0]
        if sim >= DEDUP_THRESHOLD:
            dup_id = existing["ids"][0][0]
            _EPISODIC.update(ids=[dup_id], documents=[doc], metadatas=[meta])
            return f"merged into {dup_id} (sim={sim:.2f})"

    new_id = f"s-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    _EPISODIC.upsert(ids=[new_id], documents=[doc], metadatas=[meta])
    return f"stored {new_id}"


def session_end(messages: list, mode: str, fh=None, session_id=None) -> None:
    """TODO 6: summarize, quality-check, dedup, store. Called from run_session's finally."""
    #print(f"[session_end] messages={len(messages)} episodic={_EPISODIC is not None} fh={fh is not None}")
    if not messages or _EPISODIC is None:
        return
    print("[session_end] passed guard, summarizing...")
    try:
        summary = _summarize_session(messages, mode)
        #print(f"[session_end] summary={summary}") <- DEBUG
        if not any(summary.values()):
            print("[session_end] summary empty, returning")
            return
        flag, note = _quality_check(summary, messages, mode)
        #print(f"[session_end] quality={flag}") <- DEBUG
        result = _dedup_and_store(summary, flag, mode)
        #print(f"[session_end] stored: {result}") <- DEBUG
        if fh and session_id:
            _trace(fh, session_id, "memory_write", tier="cheap",
                   message=f"session summary {result}; quality={flag}")
    except Exception as exc:
        # print(f"[session_end] EXCEPTION: {type(exc).__name__}: {exc}") <- DEBUG
        if fh and session_id:
            _trace(fh, session_id, "memory_write", success=False,
                   message=f"summary failed: {type(exc).__name__}: {exc}")

def apply_retention(now: datetime | None = None) -> dict:
    """TTL 90d + cap 50; summarize-then-archive standing preferences (Part 5)."""
    if _EPISODIC is None:
        return {"pruned": 0}
    now = now or datetime.now(timezone.utc)
    data = _EPISODIC.get(where={"user_id": ENCORE_USER_ID})
    ids, metas, docs = data["ids"], data["metadatas"], data["documents"]
    # sort newest first, cap
    rows = sorted(zip(ids, metas, docs), key=lambda r: r[1].get("timestamp", ""), reverse=True)
    prune = rows[EPISODIC_CAP:]
    for _id, meta, doc in rows[EPISODIC_CAP:]:
        ts = meta.get("timestamp", "")
        try:
            age = (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
        except ValueError:
            age = 0
        if age > EPISODIC_TTL_DAYS:
            prune.append((_id, meta, doc))
    # summarize-then-archive: extract standing preferences before delete
    prefs = kv_get(_HOT_CONN, ENCORE_USER_ID, "long_term_preferences", []) or []
    for _id, meta, doc in prune:
        for f in (meta.get("key_facts") or "").split(","):
            f = f.strip()
            if f and f not in prefs:
                prefs.append(f)
    if prefs:
        kv_set(_HOT_CONN, ENCORE_USER_ID, "long_term_preferences", prefs)
    ids_to_delete = [r[0] for r in prune]
    if ids_to_delete:
        _EPISODIC.delete(ids=ids_to_delete)
    return {"pruned": len(ids_to_delete), "long_term_preferences": len(prefs)}

#----------TRACE + LOOP HELPERS
def _trace(fh, session_id, event, **fields):
    """Write one JSONL line. Fields are optional per event (Part 6)."""
    line = {"ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id, "event": event, **fields}
    fh.write(json.dumps(line, default=str) + "\n")
    fh.flush()


def _estimate_tokens(messages) -> int:
    """Rough token estimate: ~4 chars/token. Logged, not authoritative."""
    return sum(len(str(m.get("content", ""))) for m in messages) // 4


def _rules_text() -> str:
    """The reinjected constraints (Rules block), from procedural memory (Part 6)."""
    return _extract_rules_block(load_procedural()) or "(no rules loaded)"


def _messages_with_rules(rules: str, messages: list) -> list:
    """Reinject the Rules on every call (Part 6). Rules first, then the conversation."""
    return [{"role": "system", "content": f"## Rules (always apply)\n{rules}"}] + messages

def _wrap_tool_result(result: str) -> str:
    """Delimit tool output as untrusted data (Part 5)."""
    return f"<tool_result>\n{result}\n</tool_result>"

def _approve(name: str, args: dict) -> bool:
    """Ask the user to approve a write (Part 4). export/delete always ask.

    Non-interactive callers (the eval runner) hit EOF on input(); treat that as
    a decline so the loop returns a resource error instead of crashing.
    """
    print(f"\n[approval needed] {name}({json.dumps(args)})")
    try:
        ans = input("Approve? [y/N] ").strip().lower()
    except EOFError:
        print("[approval] no interactive input; declining.")
        return False
    return ans in ("y", "yes")

def _compress(fh, session_id, mode, messages: list) -> list:
    """Rolling summarization: compress older turns, keep recent ones + hot-state block."""
    if len(messages) <= 4:
        return messages
    old, recent = messages[:-4], messages[-4:]
    prompt = ("Summarize this conversation for an agent that must continue the task. "
              "Keep: the active playlist, pending approvals, what 'that one' refers to, "
              "and any user preferences stated. Be brief.\n\n" +
              "\n".join(str(m.get("content")) for m in old if m.get("content")))
    try:
        summary = chat([{"role": "user", "content": prompt}], mode,
                       model=model_for("cheap", mode)).get("content", "")
    except requests.RequestException:
        return messages
    _trace(fh, session_id, "compress", tier="cheap",
       message=f"compressed {len(old)} messages: {summary[:200]}")
    return ([{"role": "system", "content": f"[Summary of earlier conversation]\n{summary}"}]
            + recent)

#----------SESSION START-----------
def session_start(first_message: str) -> dict:
    """TODO 3: load procedural + hot state, retrieve episodic, front-load the context.

    Returns a dict the loop (TODO 4) consumes:
    {"context": str, "objective": str, "warnings": [...], "handles": {...}}
    """
    global _HOT_CONN, _EPISODIC, _SEMANTIC
    warnings = []

    # 1. Open stores
    try:
        _HOT_CONN = open_store()
    except Exception as exc:
        warnings.append(f"hot state unavailable: {type(exc).__name__}: {exc}")
    try:
        client = open_client()
        _EPISODIC, _SEMANTIC = open_collections(client)
    except Exception as exc:
        _EPISODIC = _SEMANTIC = None
        warnings.append(f"vector memory unavailable: {type(exc).__name__}: {exc}")

    # 2. Procedural memory
    procedural = load_procedural()
    if not procedural:
        warnings.append(f"procedural memory missing at {PROMPT_PATH}; running without rules")
    rules = _extract_rules_block(procedural)

    # 3. Retrieve episodic for the first message
    episodic = retrieve_episodic(first_message)

    # 4. Hot-state block
    state_block = build_state_block()

    # 5. Front-load
    context = build_front_loaded_context(
        rules=rules,
        objective=first_message,
        state_block=state_block,
        key_facts=episodic["key_facts"],
        summaries=episodic["summaries"],
    )

    return {
        "context": context,
        "objective": first_message,
        "warnings": warnings,
        "handles": {"hot": _HOT_CONN is not None,
                    "episodic": _EPISODIC is not None,
                    "semantic": _SEMANTIC is not None},
        "key_facts": episodic["key_facts"],
        "summaries": episodic["summaries"],
    }

#----------SESSION LOOP TODO 4/5----------
def run_session(mode: str) -> None:
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(TRACE_DIR, exist_ok=True)
    trace_path = os.path.join(TRACE_DIR, f"{session_id}.jsonl")

    prompt = input("Prompt: ")
    start = session_start(prompt)                     # TODO 3
    warnings = start["warnings"]
    if warnings:
        print("[warnings]", "; ".join(warnings))

    rules = _rules_text()
    objective = start["objective"]

    # route once (Lab 4), using the cheap tier
    route = get_available_tools(objective, mode)
    available = [n for n in route["tools"] if n in TOOLS]
    schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in available]

    messages = [{"role": "user", "content": start["context"]}]
    steps = 0
    step_limit = _INJECT.get("max_steps") or MAX_STEPS # added
    tool_results_since_compress = 0

    with open(trace_path, "a", encoding="utf-8") as fh:
        _trace(fh, session_id, "memory_read", tier="cheap",
            message=f"episodic retrieval: {len(start.get('summaries', []))} summaries, "
                    f"{len(start.get('key_facts', []))} key_facts")
        _trace(fh, session_id, "route", model=model_for("cheap", mode), tier="cheap",
            message=f"domains={route['domains']} tools={available}")
        try:
            while steps < step_limit:
                steps += 1
                outbound = _messages_with_rules(rules, messages)
                t0 = time.monotonic()
                try:
                    message = chat(outbound, mode, tools=schemas or None,
                                tool_choice="auto", model=model_for("cheap", mode))
                except requests.RequestException as exc:
                    _trace(fh, session_id, "llm_call", tier="cheap", success=False,
                           error_category="transient", message=f"{type(exc).__name__}: {exc}")
                    _trace(fh, session_id, "final", message="status=stuck (llm call failed)")
                    return

                _trace(fh, session_id, "llm_call", model=model_for("cheap", mode), tier="cheap",
                    tokens_in=_estimate_tokens(outbound), tokens_out=len(
                        str(message.get("content", ""))) // 4,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    message=(message.get("content") or "")[:200])

                calls = message.get("tool_calls") or []
                if not calls:
                    # No tool call: the model answered in prose. Treat as answered, low confidence.
                    _trace(fh, session_id, "final", tier="cheap",
                        message="status=answered_low_confidence (prose; model skipped final_answer)")
                    print(message.get("content", ""))
                    return

                call = calls[0]
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                # --- final_answer: terminate (TODO 5) ---
                if name == "final_answer":
                    try:
                        jsonschema.validate(args, FINAL_ANSWER_SCHEMA["function"]["parameters"])
                    except jsonschema.ValidationError as exc:
                        _trace(fh, session_id, "final", success=False,
                            message=f"status=stuck (invalid final_answer: {exc.message}")
                        print("[stuck] final_answer failed validation.")
                        return
                    # Loop override (Part 4): model can't claim answered if budget hit
                    status = args["status"]
                    if steps >= step_limit and status not in ("budget_exceeded",):
                        status = "budget_exceeded"
                    _trace(fh, session_id, "final", tier="expensive", model=model_for("expensive", mode),
                        message=f"status={status} confidence={args['confidence']}")
                    print(args["answer"])
                    return

                # --- write approval gate (Part 4) ---
                if name in WRITE_TOOLS:
                    approved = _approve(name, args)
                    _trace(fh, session_id, "approval", tool=name, args=args,
                        success=approved, message="approved" if approved else "declined")
                    if not approved:
                        result = fail("resource", "The user declined this write.").to_json()
                        messages.append({"role": "assistant", "content": None,
                                        "tool_calls": [call]})
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                        "content": _wrap_tool_result(result)})
                        continue

                # --- dispatch + transient retry (Part 4) ---
                fn = TOOLS.get(name)
                if fn is None:
                    result = fail("input", f"Unknown tool '{name}'.").to_json()
                else:
                    attempt = 0
                    result = None
                    for delay in backoff_delays():
                        attempt += 1
                        result = fn(**args)
                        parsed = json.loads(result)
                        if parsed["success"]:
                            break
                        if parsed["error_category"] != "transient":
                            break
                        _trace(fh, session_id, "tool_call", tool=name, args=args,
                            success=False, error_category="transient", attempt=attempt,
                            message=f"retrying in {delay:.1f}s")
                        time.sleep(delay)

                parsed = json.loads(result)
                # --- hot-state updates (Part 5.1): what the next step will need ---
                if parsed["success"] and _HOT_CONN is not None and isinstance(parsed.get("data"), dict):
                    data = parsed["data"]
                    if name == "search_recordings" and data.get("recordings"):
                        hits = data["recordings"]
                        kv_set(_HOT_CONN, ENCORE_USER_ID, "last_results",
                               [{"n": i + 1, **h} for i, h in enumerate(hits)])
                        kv_set(_HOT_CONN, ENCORE_USER_ID, "last_recording", hits[0])
                    elif name == "create_playlist":
                        kv_set(_HOT_CONN, ENCORE_USER_ID, "active_playlist",
                               {"id": data.get("playlist_mbid"),
                                "title": data.get("title"),
                                "track_mbids": []})
                    elif name == "get_recent_listens" and data.get("listens"):
                        kv_set(_HOT_CONN, ENCORE_USER_ID, "last_recording",
                               data["listens"][0])
                
                _trace(fh, session_id, "tool_call", tool=name, args=args,
                    success=parsed["success"], error_category=parsed["error_category"])

                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": _wrap_tool_result(result)})
                tool_results_since_compress += 1

                # --- working-memory compression (Part 5.1) ---
                if (_estimate_tokens(messages) > TOKEN_BUDGET * COMPRESS_AT
                        or tool_results_since_compress >= COMPRESS_AFTER_RESULTS):
                    # (archive + summarize; see _compress below — simplified inline)
                    messages = _compress(fh, session_id, mode, messages)
                    tool_results_since_compress = 0

            # step limit hit without final_answer
            _trace(fh, session_id, "final", message="budget_exceeded")
            print("[budget_exceeded] Reached the step limit without a final answer.")
        finally:
            #print("[finally] calling session_end") <- DEBUG
            session_end(messages, mode, fh, session_id)

#----------TODO 7----------
def forget_user(user_id: str) -> None:
    """Delete every stored item for user_id from every store. Prints counts (Part 5)."""
    counts = {}
    global _HOT_CONN, _EPISODIC, _SEMANTIC

    # 1. Hot state (includes long_term_preferences, session_preferences, all keys)
    if _HOT_CONN is None:
        try:
            _HOT_CONN = open_store()
        except Exception as exc:
            counts["hot_state"] = f"unavailable ({type(exc).__name__})"
    if isinstance(_HOT_CONN, sqlite3.Connection):
        counts["hot_state"] = kv_forget_user(_HOT_CONN, user_id)

    # 2. Episodic (Chroma, user-scoped)
    if _EPISODIC is None:
        try:
            client = open_client()
            _EPISODIC, _SEMANTIC = open_collections(client)
        except Exception as exc:
            counts["episodic"] = f"unavailable ({type(exc).__name__})"
    if _EPISODIC is not None:
        before = _EPISODIC.count()
        _EPISODIC.delete(where={"user_id": user_id})
        counts["episodic"] = before - _EPISODIC.count()

    # 3. Semantic: NOT user-scoped (shared knowledge) — must not delete.
    #    Documented in README; not touched by --forget.

    # 4. Session set (in-memory; cleared for completeness)
    before_set = len(ENCORE_CREATED_PLAYLISTS)
    ENCORE_CREATED_PLAYLISTS.clear()
    counts["session_playlists"] = before_set

    # 5. Archived logs (raw messages archived during compression)
    archive_dir = os.path.join(os.path.dirname(__file__), "traces", "archive")
    archived = 0
    if os.path.isdir(archive_dir):
        for fname in os.listdir(archive_dir):
            path = os.path.join(archive_dir, fname)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if user_id in f.read():
                        os.remove(path)
                        archived += 1
            except OSError:
                pass
    counts["archived_logs"] = archived

    print(f"[forget] user_id={user_id!r}")
    for store, n in counts.items():
        print(f"  {store}: {n} deleted")
    print("Note: ListenBrainz data on the server was not touched.")

#----------MAIN----------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["online", "offline"])
    parser.add_argument("--forget", action="store_true")
    parser.add_argument("--now", default=None, help="ISO date to simulate time passing (retention)")
    parser.add_argument("--inject", default=None,
                        help="JSON file with transient_rate, seed, max_steps")
    args = parser.parse_args()

    if args.inject:
        load_inject(args.inject)
    if args.forget:
        forget_user(ENCORE_USER_ID)
    else:
        run_session(args.mode)