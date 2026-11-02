"""
Chapter 7 — Memory — Example 5
The Agentic AI Bible (Revised & Expanded Edition 2026)

A straight-shot ChromaDB example: vector memory.

This script:
    1. Opens a persistent Chroma database on disk
    2. Stores a few session summaries (episodic memory)
       with metadata: user_id, timestamp, topics
    3. Searches them by meaning, filtered by user_id
    4. Shows a second collection for user-independent
       facts (semantic memory)
    5. Deletes everything for one user (the "forget" path)

No LLM, no agent loop. Just the vector store.

Chroma embeds text for you with its default model
(all-MiniLM-L6-v2). The first run downloads it
(about 80 MB), so it takes a moment.

Setup:

    pip install chromadb

Run:

    python ch07_05_chroma_simple.py
"""

from datetime import datetime, timezone

import chromadb


#
# 1. Open (or create) a database folder on disk.
#    Run the script twice: the data is still there.
#

client = chromadb.PersistentClient(path="./ch07_chroma_demo")

# cosine distance: 0 = same meaning, 2 = opposite
episodic = client.get_or_create_collection(
    name="episodic",
    metadata={"hnsw:space": "cosine"},
)


#
# 2. Store session summaries.
#
#    upsert = insert, or replace if the id exists,
#    so re-running the script does not duplicate rows.
#
#    Metadata values must be str, int, float, or bool
#    (no lists), so topics is a comma-separated string.
#

now = datetime.now(timezone.utc).isoformat()

episodic.upsert(
    ids=["s1", "s2", "s3", "s4"],
    documents=[
        "Built a 40-minute instrumental focus playlist for "
        "studying. User wanted no vocals.",
        "User said they dislike Christmas music and asked to "
        "never include holiday songs.",
        "Made an upbeat running playlist around 170 BPM.",
        "Different user: asked for sad acoustic songs.",
    ],
    metadatas=[
        {"user_id": "alice", "timestamp": now, "topics": "focus,playlist"},
        {"user_id": "alice", "timestamp": now, "topics": "preferences"},
        {"user_id": "alice", "timestamp": now, "topics": "running,playlist"},
        {"user_id": "bob",   "timestamp": now, "topics": "acoustic"},
    ],
)

print(f"episodic collection holds {episodic.count()} summaries\n")


#
# 3. Search by meaning.
#
#    The query shares almost no words with s1, but
#    the embedding still finds it. The where filter
#    keeps Bob's memories out of Alice's results.
#

query = "music to concentrate on while doing homework"

results = episodic.query(
    query_texts=[query],
    n_results=2,
    where={"user_id": "alice"},
)

print(f'Q: "{query}"  (user_id = alice)')

# Results are lists of lists: one inner list per query text.
for doc, meta, dist in zip(
    results["documents"][0],
    results["metadatas"][0],
    results["distances"][0],
):
    similarity = 1 - dist
    print(f"    {similarity:.2f}  [{meta['topics']}]  {doc}")


#
# 4. Semantic memory: facts that don't depend on the
#    user go in a SEPARATE collection, with the source
#    they came from.
#

knowledge = client.get_or_create_collection(
    name="semantic",
    metadata={"hnsw:space": "cosine"},
)

knowledge.upsert(
    ids=["kb1", "kb2", "kb3"],
    documents=[
        "Shoegaze: guitar-heavy rock with washes of distortion "
        "and reverb, and vocals buried in the mix.",
        "Apple Music does not report listens to ListenBrainz "
        "by itself; you need a scrobbler app or extension.",
        "A B-side is the secondary track released alongside "
        "a single.",
    ],
    metadatas=[
        {"source": "glossary"},
        {"source": "listenbrainz-docs"},
        {"source": "glossary"},
    ],
)

query = "why are my iPhone plays missing from my history?"

results = knowledge.query(query_texts=[query], n_results=1)

print(f'\nQ: "{query}"  (semantic)')
print(
    f"    {1 - results['distances'][0][0]:.2f}  "
    f"[{results['metadatas'][0][0]['source']}]  "
    f"{results['documents'][0][0]}"
)


#
# 5. Forget a user: delete every row with their user_id.
#

before = episodic.count()
episodic.delete(where={"user_id": "alice"})
after = episodic.count()

print(f"\nforget alice: deleted {before - after}, {after} left (bob's)")

left = episodic.get(where={"user_id": "alice"})
print(f"rows remaining for alice: {len(left['ids'])}")
