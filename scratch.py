import encore
from memory.hot_state import open_store, kv_set, kv_get, kv_forget_user
from memory.stores import open_client, open_collections

# ---------------------------------------------------------------- audit
tools_names = set(encore.TOOLS)
schema_names = {s["function"]["name"] for s in encore.TOOL_SCHEMAS}

print("TOOLS count:", len(tools_names))
print("SCHEMAS count:", len(schema_names))
print("in TOOLS not schemas:", tools_names - schema_names)
print("in schemas not TOOLS:", schema_names - tools_names)

print("WRITE_TOOLS - TOOLS:", encore.WRITE_TOOLS - tools_names)
print("WRITE_TOOLS ∩ TOOLS:", encore.WRITE_TOOLS & tools_names)

for name in ["JSPF_EXT", "_track_ref", "ENCORE_CREATED_PLAYLISTS", "EXPORT_SERVICES"]:
    print(f"{name}: {hasattr(encore, name)}")

# ---------------------------------------------------------------- hot state smoke
print("\n--- hot state ---")
c = open_store()
kv_set(c, "smoke", "k", {"x": 1})
print("get:", kv_get(c, "smoke", "k"))
print("forgot:", kv_forget_user(c, "smoke"))
print("after forget:", kv_get(c, "smoke", "k", default="(gone)"))
c.close()

# ---------------------------------------------------------------- chroma smoke
print("\n--- chroma (episodic + semantic) ---")
client = open_client()
episodic, semantic = open_collections(client)

episodic.upsert(
    ids=["smoke-1"],
    documents=["built a focus playlist, user wanted no vocals"],
    metadatas=[{"user_id": "smoke", "timestamp": "2026-01-01", "topics": "focus"}],
)
res = episodic.query(
    query_texts=["music to study to"],
    n_results=1,
    where={"user_id": "smoke"},
)
print("episodic hit:", res["documents"][0][0], "| user_id:", res["metadatas"][0][0]["user_id"])

semantic.upsert(
    ids=["smoke-kb-1"],
    documents=["A B-side is the secondary track released alongside a single."],
    metadatas=[{"source": "glossary"}],
)
res = semantic.query(query_texts=["what is a b-side"], n_results=1)
print("semantic hit:", res["documents"][0][0], "| source:", res["metadatas"][0][0]["source"])

# cleanup so the smoke rows don't linger
episodic.delete(where={"user_id": "smoke"})
semantic.delete(ids=["smoke-kb-1"])
print("cleaned up: episodic remaining for smoke =",
      len(episodic.get(where={"user_id": "smoke"})["ids"]),
      "| semantic has smoke-kb-1:",
      "smoke-kb-1" in semantic.get(ids=["smoke-kb-1"])["ids"])

print("\n--- session_start (no memories yet) ---")
enc = encore
res = enc.session_start("Build me a focus playlist")
print("warnings:", res["warnings"])
print("handles:", res["handles"])
print("objective:", res["objective"])
print("--- context (first 500 chars) ---")
print(res["context"][:500])
print("--- key_facts ---", res.get("key_facts"))

print("\n--- episodic after session ---")
from memory.stores import open_client, open_collections
import encore as enc

c = open_client()
e, s = open_collections(c)
print("episodic count:", e.count())
print("for user:", len(e.get(where={"user_id": enc.ENCORE_USER_ID})["ids"]))
docs = e.get(where={"user_id": enc.ENCORE_USER_ID})["documents"]
for d in docs[:3]:
    print("  -", d[:120])

# append to scratch.py, or in a REPL
res = encore.session_start("Build me a focus playlist")
print("has memories block:", "## Relevant past sessions" in res["context"])
print("has staleness:", "may be outdated" in res["context"])

print("\n--- after forget ---")
c = open_client(); e, s = open_collections(c)
print("episodic count:", e.count())
print("for user:", len(e.get(where={"user_id": enc.ENCORE_USER_ID})["ids"]))
print("hot_state prefs:", kv_get(open_store(), enc.ENCORE_USER_ID, "session_preferences", []))

from memory.hot_state import open_store, kv_get
c = open_store()
print("session_preferences:", kv_get(c, encore.ENCORE_USER_ID, "session_preferences", []))
print("last_recording:", kv_get(c, encore.ENCORE_USER_ID, "last_recording", None))
print("tool_calls:", kv_get(c, encore.ENCORE_USER_ID, "tool_calls", None))