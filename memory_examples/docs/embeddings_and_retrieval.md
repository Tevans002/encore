# Embeddings, Chunking, and Retrieval Evaluation

This guide covers semantic memory for the midterm (Part 5): turning documents
into chunks, embedding them, measuring retrieval, the two improvements
(hybrid search and contextual prefixes), and the **bonus** for using your own
embedding model.

Runnable examples: `ch07_07_chunking_experiment.py` (chunk sizes + recall),
`ch07_08_embed_pdf.py` (a real PDF, end to end). Chroma API details are in
`chromadb.md`.

---

## 1. What an embedding is

An embedding model turns a piece of text into a fixed-length list of numbers
(a vector) so that texts with similar meaning get vectors that point in
similar directions. Retrieval embeds the question the same way and returns
the stored chunks whose vectors are closest (cosine similarity).

Three facts drive almost every design decision:

1. **Query and documents must use the same model.** Vectors from different
   models aren't comparable, and nothing warns you when you mix them.
2. **Every model has a maximum input length.** Text past the limit is
   silently dropped. Chroma's default, all-MiniLM-L6-v2, reads **256
   tokens**, including two special tokens it adds to every input.
3. **Tokens are not words.** English averages about 1.3 tokens per word,
   and identifiers like MBIDs or `/1/explore/lb-radio` split into many
   tokens. Count with the model's own tokenizer, as `ch07_07` does.

## 2. Chunking

A chunk is the unit you embed and retrieve. Too big and one vector has to
represent several topics (and may be truncated); too small and a chunk lacks
the context to match a question.

**Rule for this project:** every chunk must fit inside your embedding
model's limit. With the default model that means **256 tokens or fewer**, so
compare sizes such as 64 / 128 / 256 rather than 512.

### Strategy A: fixed-size token windows

Cut every N tokens with some overlap so a fact isn't split across a
boundary. `chunk_by_tokens()` in `ch07_07` does this, cutting only on whole
words and checking the limit:

```python
from ch07_07_chunking_experiment import load_model_tokenizer, chunk_by_tokens
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

tok = load_model_tokenizer(DefaultEmbeddingFunction())
chunks = chunk_by_tokens(tok, text, chunk_tokens=256, overlap=32)
```

### Strategy B: section-based

Split on the document's own structure (headings, one API endpoint per
chunk), so each chunk is about one thing. If a section is still longer than
the limit, sub-split it with Strategy A and repeat the section title at the
top of each piece:

```python
import re

def split_sections(markdown_text):
    """Split at '#', '##', '###' headings; keep the heading with its body."""
    parts = re.split(r"(?m)^(?=#{1,3} )", markdown_text)
    return [p.strip() for p in parts if p.strip()]

chunks = []
for section in split_sections(text):
    title = section.splitlines()[0]
    if token_count(tok, section) <= 256:
        chunks.append(section)
    else:
        for piece in chunk_by_tokens(tok, section, 256 - token_count(tok, title), 32):
            chunks.append(f"{title}\n{piece}" if not piece.startswith(title) else piece)
```

Getting clean text out of the docs is often the hardest step. ReadTheDocs
pages and PDFs carry navigation, headers and footers that repeat on every
page. Remove them before embedding (see `clean_page()` in `ch07_08`), or
every chunk will look similar to every question.

**Store where each chunk came from** (`source`, `section`, `page`) in its
metadata so the agent can cite it.

## 3. Evaluating retrieval (top-3 recall)

Build the evaluation set **before** tuning anything:

- At least 20 questions, phrased the way a user would ask (don't copy the
  doc's wording, or keyword overlap does the work for you).
- For each, record what answers it: either a short exact phrase from the
  docs (what `ch07_07` uses), or the section/chunk ID.
- A question is a hit at k if any of the top k retrieved chunks contains the
  answer. **Top-3 recall** = hits / questions.

Run every configuration on the **same corpus and the same questions**, and
change one thing at a time. Report the numbers in the Workbook table.

```bash
python ch07_07_chunking_experiment.py --docs ./kb_text --questions eval.json --sizes 128 256 --show-misses
```

With 20 questions, one question is 5 percentage points, so treat
differences of one or two questions as noise. Read the misses: they tell
you whether the problem is chunking (answer split or buried), wording
(question and answer use different terms), or an identifier (exact string
the model doesn't represent well).

## 4. Improvement 1: hybrid search (BM25 + vectors)

Vector search finds meaning; keyword search finds exact strings like MBIDs,
endpoint paths, and header names. Hybrid search combines them.

```bash
pip install rank_bm25
```

```python
import re
from rank_bm25 import BM25Okapi

def tokenize(text):
    # keep paths and identifiers together: "/1/explore/lb-radio", "x-ratelimit-reset-in"
    return re.findall(r"[a-z0-9_./-]+", text.lower())

# Build once, alongside the Chroma collection (same chunks, same ids, same order).
bm25 = BM25Okapi([tokenize(c) for c in chunks])

def hybrid_search(question, k=3, pool=20, rrf_k=60):
    # 1. vector ranking
    res = semantic.query(query_texts=[question], n_results=pool)
    vec_rank = {cid: r for r, cid in enumerate(res["ids"][0])}

    # 2. keyword ranking
    scores = bm25.get_scores(tokenize(question))
    order = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)[:pool]
    kw_rank = {chunk_ids[i]: r for r, i in enumerate(order)}

    # 3. Reciprocal Rank Fusion: no score normalization needed
    fused = {}
    for ranks in (vec_rank, kw_rank):
        for cid, r in ranks.items():
            fused[cid] = fused.get(cid, 0) + 1 / (rrf_k + r + 1)
    return sorted(fused, key=fused.get, reverse=True)[:k]
```

Reciprocal Rank Fusion only needs each method's ranking, so you don't have
to put BM25 scores and cosine similarities on the same scale. If you prefer
a weighted sum, min-max normalize both score lists first and tune the
weight on your evaluation set.

## 5. Improvement 2: contextual prefixes

A chunk like "Set mode to easy, medium or hard" doesn't say it's about LB
Radio. Have the cheap model write one sentence of context per chunk,
**embed the sentence plus the chunk**, but keep the original text as the
document you return:

```python
context = cheap_model(f"Document: {doc_title}\nSection: {section}\nChunk: {chunk}\n"
                      "In one sentence, say what this chunk is about and where it fits.")
semantic.upsert(
    ids=[chunk_id],
    documents=[chunk],                           # what the agent reads
    embeddings=ef([f"{context}\n{chunk}"]),      # what the search matches
    metadatas=[{"source": src, "section": section, "context": context}],
)
```

Make sure `context + chunk` still fits the model's limit, and cache the
generated sentences: rebuilding the index shouldn't re-call the LLM for
every chunk.

---

## 6. ⭐ Bonus: your own embedding model

Chroma embeds with all-MiniLM-L6-v2 unless you tell it otherwise. For bonus
credit, choose a different embedding model, plug it into Chroma, and show
with your evaluation set whether it retrieves better.

### What earns the bonus

1. A non-default embedding model used for your semantic store (episodic
   too, if you like) through a Chroma embedding function.
2. Top-3 recall for **both** models on the **same corpus, chunks and
   questions**, in the Workbook.
3. Every chunk within the new model's token limit, checked in code.
4. The model name **and version/revision** in metadata, and your startup
   check refusing to mix it with the old embeddings.
5. A short justification in the README: recall gained vs. the cost in
   download size, embedding time, and memory.

### Choosing a model

All of these run locally on a laptop CPU through `sentence-transformers`
(no API key, no per-call cost). Sizes are approximate downloads.

| Model | Vector size | Max tokens | Size | Notes |
| --- | --- | --- | --- | --- |
| `all-MiniLM-L6-v2` (Chroma default) | 384 | 256 | ~90 MB | Baseline |
| `all-mpnet-base-v2` | 768 | 384 | ~420 MB | Stronger general-purpose model, slower |
| `BAAI/bge-small-en-v1.5` | 384 | 512 | ~130 MB | Good retrieval quality for its size |
| `BAAI/bge-base-en-v1.5` | 768 | 512 | ~440 MB | Larger BGE |
| `nomic-ai/nomic-embed-text-v1.5` | 768 | 8192 | ~550 MB | Long inputs; needs `trust_remote_code=True` and the `search_document: ` / `search_query: ` prefixes |

Check each model's card on Hugging Face for its exact limit and any
recommended query prefix before you trust this table.

```bash
pip install sentence-transformers      # also installs PyTorch (large download)
```

The first use downloads the model from Hugging Face into
`~/.cache/huggingface/`. Do it at home, not on campus Wi-Fi during class.

### Option A: Chroma's built-in wrapper (simplest)

```python
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

ef = SentenceTransformerEmbeddingFunction(
    model_name="BAAI/bge-small-en-v1.5",
    normalize_embeddings=True,
)
semantic = client.get_or_create_collection(
    "semantic_bge_small",                  # new name: never reuse a MiniLM collection
    embedding_function=ef,
    metadata={"hnsw:space": "cosine", "embedding_model": "BAAI/bge-small-en-v1.5"},
)
```

Chroma saves this function in the collection's configuration and can
rebuild it when you reopen the collection. Pass `embedding_function=ef`
anyway.

### Option B: your own embedding function (full control)

Write your own when you need the token limit, a version string, or
different handling of queries and documents (some models want a prefix on
queries):

```python
from chromadb import Documents, EmbeddingFunction, Embeddings
from chromadb.utils.embedding_functions import register_embedding_function
from sentence_transformers import SentenceTransformer


@register_embedding_function
class EncoreEmbedder(EmbeddingFunction[Documents]):

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5",
                 query_prefix: str = "", doc_prefix: str = ""):
        self.model_name = model_name
        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.model = SentenceTransformer(model_name)

    # Documents being stored.
    def __call__(self, input: Documents) -> Embeddings:
        texts = [self.doc_prefix + t for t in input]
        return self.model.encode(texts, normalize_embeddings=True).tolist()

    # Questions being searched (Chroma 1.x calls this for query_texts).
    def embed_query(self, input: Documents) -> Embeddings:
        texts = [self.query_prefix + t for t in input]
        return self.model.encode(texts, normalize_embeddings=True).tolist()

    # Helpers for your chunker and your metadata.
    @property
    def max_tokens(self) -> int:
        return self.model.max_seq_length

    def count_tokens(self, text: str) -> int:
        return len(self.model.tokenizer(text)["input_ids"])

    # Let Chroma save and rebuild this function with the collection.
    @staticmethod
    def name() -> str:
        return "encore-sentence-transformer"

    def get_config(self) -> dict:
        return {"model_name": self.model_name,
                "query_prefix": self.query_prefix,
                "doc_prefix": self.doc_prefix}

    @staticmethod
    def build_from_config(config: dict) -> "EncoreEmbedder":
        return EncoreEmbedder(**config)


ef = EncoreEmbedder("BAAI/bge-small-en-v1.5")
print(ef.model_name, "reads at most", ef.max_tokens, "tokens")
```

For `nomic-embed-text-v1.5` you would use
`EncoreEmbedder("nomic-ai/nomic-embed-text-v1.5", query_prefix="search_query: ",
doc_prefix="search_document: ")` and pass `trust_remote_code=True` to
`SentenceTransformer`.

The class name, `name()` and the config keys become part of the saved
collection. If you change them later, open the collection with the same
values or re-index.

### Running a fair comparison

- **Same chunks for both models first.** Chunk at ≤ 256 tokens so the
  default model reads every chunk in full, embed the same chunks with both
  models, and compare top-3 recall. That isolates the model.
- **Then, optionally, use the new model's headroom.** If it reads 512
  tokens, try 384- or 512-token chunks *with the new model only* and report
  that as a separate row.
- **One collection per model** (and per chunk size). Name collections so
  the model is obvious: `semantic_minilm_256`, `semantic_bge_small_256`.
- Measure embedding time for the whole corpus on your laptop and note it.
  A model that's 3 points better but 10× slower may not be worth it for
  episodic writes at the end of every session.

To reuse `ch07_07` with another model, replace its `DefaultEmbeddingFunction()`
and `load_model_tokenizer()` with your embedder and its tokenizer, and set
`MODEL_LIMIT` to the new model's limit.

### Switching models later (the re-index procedure)

1. Bump the model name/version in your config.
2. Create **new** collections for the new model. Never add new-model
   vectors to an old collection.
3. Re-embed the knowledge base from your source files.
4. For episodic memory, re-embed the stored summary **texts** (`get()` them
   from the old collection, `upsert()` into the new one with the new
   `embedding_model` metadata).
5. Rerun your retrieval evaluation, then delete the old collections.

Put this procedure in your README; it's also what the "embedding model
change" failure mode in Part 5 asks for.
