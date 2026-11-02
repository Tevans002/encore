# ChromaDB Guide for Encore

ChromaDB is the vector store behind Encore's **episodic** memory (session
summaries) and **semantic** memory (ListenBrainz/MusicBrainz docs and your
glossary). This guide covers what you need for the midterm. Runnable
examples: `ch07_05_chroma_simple.py`, `ch07_07_chunking_experiment.py`,
`ch07_08_embed_pdf.py`.

Tested with chromadb 1.5. Official docs: https://docs.trychroma.com

---

## 1. The mental model

| Chroma term | What it is | In Encore |
| --- | --- | --- |
| **Client** | A connection to a database folder | One per process, opened at startup |
| **Collection** | A named table of records | `episodic`, `semantic` (keep them separate) |
| **Record** | `id` + `document` (text) + `metadata` (dict) + `embedding` (vector) | One session summary, or one doc chunk |
| **Embedding function** | Turns text into a vector | Chroma's default model unless you choose one |

When you `add` text, Chroma embeds it and stores the vector. When you
`query` with text, Chroma embeds the question and returns the records whose
vectors are closest.

---

## 2. Open a database

```python
import chromadb

client = chromadb.PersistentClient(path="./memory/chroma")   # saved on disk
# client = chromadb.EphemeralClient()                       # in memory, gone at exit (tests)
```

- Put the folder under `memory/`. It's already in the starter's `.gitignore`.
- Open the client **once** and reuse it. Don't open a new client per tool call.

## 3. Create or open a collection

```python
episodic = client.get_or_create_collection(
    name="episodic",
    metadata={"hnsw:space": "cosine"},     # distance metric, set at creation
)
```

- **Names:** 3–512 characters from `a-z A-Z 0-9 . _ -`, starting and ending
  with a letter or digit. `"t"` is rejected; `"episodic"` is fine.
- **Distance:** always use cosine for text. It can only be set when the
  collection is created; changing it later means making a new collection.
- **Cosine distance → similarity:** `similarity = 1 - distance`. 0 distance
  means identical meaning. Your dedup threshold (0.92–0.95 similarity) is a
  distance of 0.05–0.08.

## 4. Write records

```python
episodic.upsert(
    ids=[session_id],
    documents=[summary_text],
    metadatas=[{
        "user_id": user_id,
        "timestamp": "2026-10-03T18:22:05+00:00",
        "ts": 1791051725,                   # unix seconds: easy to filter by time
        "topics": "focus playlist,export",
        "quality": "ok",                    # or "flagged"
        "embedding_model": "all-MiniLM-L6-v2",
    }],
)
```

> ⚠ **`add` silently ignores an ID that already exists.** The old text stays
> and no error is raised. Use **`upsert`** to insert-or-replace, and
> `update` to change fields of an existing record. This matters for dedup
> merges: a merged summary written with `add` under the old ID is lost.

**Metadata rules**

- Values must be `str`, `int`, `float` or `bool`. Chroma 1.x also accepts a
  list of those; older versions don't, so a comma-separated string is the
  portable choice.
- `None` is rejected. Leave the key out instead.
- Store times as numbers (`ts`) when you want range filters; ISO strings
  can't be compared with `$gt`.

## 5. Query by meaning

```python
res = episodic.query(
    query_texts=["the focus playlist from last week"],
    n_results=5,
    where={"user_id": user_id},
    include=["documents", "metadatas", "distances"],
)

for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
    print(round(1 - dist, 3), meta["timestamp"], doc[:80])
```

Results are **lists of lists**, one inner list per query text, so with a
single question you read index `[0]`.

`n_results` larger than the number of matching records is fine in Chroma
1.x, but on an empty collection guard it yourself (`if col.count() == 0`).

### Filters (`where`)

```python
where={"user_id": "alice"}                                      # equals
where={"ts": {"$gte": cutoff}}                                   # $gt $gte $lt $lte $ne
where={"quality": {"$in": ["ok", "flagged"]}}                    # $in $nin
where={"$and": [{"user_id": "alice"}, {"ts": {"$gte": cutoff}}]} # combine; also $or
```

`$and`/`$or` need **two or more** conditions. With just one, write it
directly.

Filter on the text itself with `where_document`:

```python
col.get(where_document={"$contains": "/1/explore/lb-radio"})
```

## 6. Read, update, delete without a query

```python
col.get(ids=["s1"])                                   # by id
col.get(where={"user_id": "alice"}, include=["metadatas"])
col.get(ids=["s1"], include=["embeddings"])           # the stored vector
col.update(ids=["s1"], metadatas=[{"quality": "flagged"}])
col.delete(ids=["s1"])
col.delete(where={"user_id": "alice"})                # the --forget path
col.count()
client.delete_collection("episodic")                  # drop the whole collection
```

For `--forget`, count before and after (or `get` the matching IDs first) so
you can print how many were deleted, then `get(where=...)` again to prove
nothing is left.

---

## 7. Encore recipes

### Episodic read path: filter, then re-rank by recency

Chroma ranks by similarity only. Fetch more than you need, then apply your
own score:

```python
import math, time

def recall(question, user_id, k=3, alpha=0.7, half_life_days=30, window_days=90):
    cutoff = time.time() - window_days * 86400
    res = episodic.query(
        query_texts=[question], n_results=10,
        where={"$and": [{"user_id": user_id}, {"ts": {"$gte": cutoff}}]},
    )
    scored = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        similarity = 1 - dist
        age_days = (time.time() - meta["ts"]) / 86400
        recency = math.exp(-math.log(2) * age_days / half_life_days)   # 1.0 now, 0.5 after one half-life
        scored.append((alpha * similarity + (1 - alpha) * recency, meta, doc))
    return sorted(scored, key=lambda t: t[0], reverse=True)[:k]
```

### Dedup on write

```python
res = episodic.query(query_texts=[new_summary], n_results=1, where={"user_id": user_id})
if res["ids"][0] and 1 - res["distances"][0][0] >= DEDUP_THRESHOLD:
    old_id = res["ids"][0][0]
    merged = merge(res["documents"][0][0], new_summary)     # your merge logic
    episodic.upsert(ids=[old_id], documents=[merged], metadatas=[new_meta])
else:
    episodic.upsert(ids=[session_id], documents=[new_summary], metadatas=[new_meta])
```

### Semantic memory with sources

Store every chunk with where it came from, so `search_music_knowledge` can
return it and `final_answer` can cite it:

```python
metadatas=[{"source": "listenbrainz-docs", "section": "API > LB Radio", "page": 42, "chunk": 3}]
```

### The embedding-model check at startup

Write the model name (and version) into each record's metadata, then at
startup:

```python
seen = {m.get("embedding_model") for m in episodic.get(include=["metadatas"])["metadatas"]}
if seen - {EMBEDDING_MODEL}:
    raise SystemExit(f"Collection was embedded with {seen}, config says {EMBEDDING_MODEL}. Re-index first.")
```

Chroma 1.x also remembers the embedding function a collection was created
with and raises `ValueError: Embedding function conflict` if you open it
with a different one. Don't rely on that alone: it doesn't catch a version
change of the same model, and your README has to describe the check.

---

## 8. Embedding functions

By default Chroma uses **all-MiniLM-L6-v2** (384-dimensional vectors, runs
on CPU via ONNX, about 80 MB downloaded to `~/.cache/chroma/` on first use).
It reads **at most 256 tokens** per text and silently drops the rest.

To use a different model, pass `embedding_function=` when you create the
collection **and every time you open it again**. Chroma 1.x can rebuild a
*registered* embedding function from the collection's saved configuration,
but an unregistered custom class, or an older Chroma version, falls back to
the default model, and your queries would then be embedded with a different
model than your documents. Passing it every time is the safe habit. See
`embeddings_and_retrieval.md` for choosing a model (the bonus).

## 9. Common problems

| Symptom | Cause / fix |
| --- | --- |
| First run hangs for a while | Downloading the default model. Wait once; later runs are offline. |
| `InvalidArgumentError: Expected a name containing 3-512 characters` | Collection name too short or has spaces/slashes. |
| Re-running doubles the data | You used `add` with new random IDs. Use stable IDs and `upsert`. |
| Updated text didn't change | `add` with an existing ID is ignored. Use `upsert` / `update`. |
| `Cannot convert Python object to MetadataValue` | A `None` or nested dict in metadata. |
| `ValueError: Embedding function conflict` | Opened a collection with a different embedding function than it was built with. Re-index into a new collection. |
| Results look random | Mixed models in one collection, or chunks far longer than 256 tokens. |
| Every chunk looks similar to every question | Boilerplate (headers, footers, nav) left in the text. Clean before embedding. |
