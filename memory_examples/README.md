# Memory Examples

Short, runnable examples of every store Encore's memory uses, plus guides
that explain each one in enough depth to build your own. They come from
Chapter 7 of *The Agentic AI Bible* ("Memory: Short-term, Long-term,
Episodic, Semantic").

None of these scripts call an LLM or run an agent loop. Each one writes to
a store, queries it, and cleans up, so you can see exactly what the
database does before you wire it into `encore.py`.

## Which store for which memory

| Encore memory (Part 2) | What it holds | Store | Example | Guide |
| --- | --- | --- | --- | --- |
| Working: hot state | Active playlist, "that song", pending approval, counters | SQLite (or Redis) | `ch07_06_sqlite_hot_store.py` | `docs/hot_state.md` |
| Episodic | Structured summaries of past sessions, filtered by `user_id` | ChromaDB collection | `ch07_05_chroma_simple.py` | `docs/chromadb.md` |
| Semantic | ListenBrainz/MusicBrainz docs and your glossary, chunked | A second ChromaDB collection | `ch07_05`, `ch07_07`, `ch07_08` | `docs/embeddings_and_retrieval.md` |
| Graph (bonus) | Band membership and collaborations | Neo4j | `ch07_04_neo4j_simple.py` | `docs/neo4j.md`, `NEO4J_SETUP.md` |

Procedural memory is your versioned `prompts/system_v1.md`; it needs no
database.

## Files

| File | What it shows | Needs |
| --- | --- | --- |
| `ch07_04_neo4j_simple.py` | Graph memory: build a small band/artist graph, then answer "who's in this band?", "who has this drummer played with?", and "who guests on this band's songs?" | A running Neo4j server |
| `ch07_05_chroma_simple.py` | Vector memory: episodic and semantic collections, metadata, search filtered by `user_id`, and a forget-a-user delete | `chromadb` |
| `ch07_06_sqlite_hot_store.py` | Key-value hot store: set/get/delete, JSON values, TTL, persistence across restarts, forget-a-user. Redis equivalent in comments | Nothing (standard library) |
| `ch07_07_chunking_experiment.py` | Chunking experiment: split documents into 64/128/256-token chunks, embed each size into its own collection, and compare recall@1 and recall@3 on an evaluation set | `chromadb` |
| `ch07_08_embed_pdf.py` | Embedding a real document: extract a PDF page by page, strip the repeating header, chunk, store with page numbers, and answer questions with page citations | `chromadb`, `pypdf` |
| `CrowdStrike-Frontier-AI-Readiness.pdf` | Example document for `ch07_08` | — |
| `whitepaper_questions.json` | 20 evaluation questions for the white paper, for use with `ch07_07` | — |
| `docs/` | Guides: `chromadb.md`, `embeddings_and_retrieval.md` (incl. the own-embedding-model bonus), `neo4j.md`, `hot_state.md` | — |
| `NEO4J_SETUP.md` | How to run Neo4j in Docker or install it locally | — |
| `docker-compose.yml` | One-command Neo4j for the examples | Docker |
| `requirements.txt` | Python packages for these examples | — |

## Setup

Python 3.10 or newer. Run everything **from this folder**, since the
examples create their data folders next to themselves:

```bash
cd memory_examples
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Running the examples

Suggested order: `06` (no setup), `05`, `08`, `07`, then `04` if you're
doing the graph bonus.

### `ch07_06_sqlite_hot_store.py`: no setup at all

```bash
python ch07_06_sqlite_hot_store.py
```

Creates `ch07_hot_store.db` in this folder. Delete the file to start over.

### `ch07_05_chroma_simple.py`: ChromaDB

```bash
python ch07_05_chroma_simple.py
```

- The first run downloads Chroma's default embedding model
  (about 80 MB), so it pauses for a moment. After that it runs offline.
- Data is saved in `./ch07_chroma_demo/`. Delete the folder to start over.

### `ch07_08_embed_pdf.py`: embedding a white paper

Example document: *Five Steps for Frontier AI Security Readiness*
(CrowdStrike, 2026), included in this folder.

```bash
python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf
python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf --ask "Why is CVSS severity not enough?"
```

It prints the top three chunks for each question with the page they came
from. Chunks are stored in `./ch07_pdf_store/`, one collection per chunk
size (`--chunk-size` picks 64, 128 or 256). Later runs reuse the stored
embeddings only if the chunks are unchanged: the script fingerprints the
chunks and re-embeds automatically when your cleaning or chunking code
produces different ones. `--rebuild` forces a re-embed.

Two things this example teaches:

- **Clean before you embed.** Every page of the paper starts with the same
  header ("Five Steps for Frontier AI Security Readiness CROWDSTRIKE" plus a
  page number). Left in, it lands in every chunk and makes unrelated chunks
  look alike. The script strips it; another document needs its own pattern.
- **Keep the source.** Chunking page by page means every chunk can cite its
  page, which is what lets `search_music_knowledge` return a source.

### `ch07_07_chunking_experiment.py`: chunk size vs. retrieval

Holds the embedding model fixed (Chroma's default, all-MiniLM-L6-v2) and
changes only the chunk size.

```bash
python ch07_07_chunking_experiment.py                 # built-in sample guide
python ch07_07_chunking_experiment.py --show-misses   # also print each miss
```

On the white paper:

```bash
python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf --export-text whitepaper_text
python ch07_07_chunking_experiment.py --docs whitepaper_text --questions whitepaper_questions.json --show-misses
```

On your own knowledge base (Part 5 semantic memory):

```bash
python ch07_07_chunking_experiment.py --docs ./my_docs --questions my_questions.json
```

`my_questions.json` is a list of `{"question": ..., "answer": ...}`, where
`answer` is a short piece of text that appears word for word in the
documents. A question is a hit at k when one of the top k chunks contains
its answer.

Things to know:

- **Chunk sizes stop at 256.** The model reads at most 256 tokens and
  silently ignores the rest, so a bigger chunk would measure truncation,
  not chunking. The script refuses sizes over 256 and checks every chunk.
- **Sizes are measured with the model's own tokenizer** and include the two
  special tokens every input gets, so a "256" chunk holds at most 254 tokens
  of text. Chunks start and end on whole words. Tokens are not words;
  English runs about 1.3 tokens per word.
- **Neighboring chunks share 16 tokens** (`--overlap`) so a short answer
  isn't split across a boundary.
- The built-in guide is about 900 words (roughly 1,200 tokens) and has 14
  questions. That is enough to see the method, but differences of one or
  two questions are noise. Use 20+ questions before drawing conclusions.
- Using a different embedding model is a bonus; see
  `docs/embeddings_and_retrieval.md`.

### `ch07_04_neo4j_simple.py`: Neo4j (graph bonus)

1. Start Neo4j (details and local-install options in `NEO4J_SETUP.md`):

   ```bash
   mkdir neo4j_data
   docker compose up -d
   docker compose logs -f     # wait for "Started.", then Ctrl+C
   ```

2. Run the example:

   ```bash
   python ch07_04_neo4j_simple.py
   ```

3. Optional: open http://localhost:7474 (`neo4j` / `password`) and run
   `MATCH (n:Ch07Demo) RETURN n` to see the graph. (This needs port 7474
   published, which the Compose file does.)

4. When you're done: `docker compose stop`.

If your Neo4j uses a different address or password, set these first:

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=password
```

(Windows PowerShell: `$env:NEO4J_PASSWORD="password"`.)

## Optional: Redis instead of SQLite

`ch07_06` ends with a commented Redis version of the same hot store, and
`docs/hot_state.md` covers it. To try it:

```bash
docker run -d --name redis -p 6379:6379 redis
pip install redis
```

## Don't commit local data

The starter's `.gitignore` already excludes these example data folders:

```
memory_examples/ch07_chroma_demo/
memory_examples/ch07_pdf_store/
memory_examples/ch07_hot_store.db
memory_examples/whitepaper_text/
memory_examples/neo4j_data/
memory_examples/.venv/
```
