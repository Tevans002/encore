"""
Chapter 7 — Memory — Example 8
The Agentic AI Bible (Revised & Expanded Edition 2026)

Embedding a real document: a PDF white paper.

This is the pipeline behind semantic memory (RAG):

    PDF  ->  text per page  ->  clean  ->  chunk  ->  embed + store
                                                          |
                     question  ->  embed  ->  nearest chunks + page numbers

What the script does:

    1. Extract the text of every page with pypdf
    2. Clean it: strip the running header that repeats on
       every page, and collapse whitespace
    3. Split each page into token chunks (default 128) using
       the embedding model's own tokenizer, so no chunk is
       ever longer than the model can read
    4. Store the chunks in a persistent Chroma collection,
       with the page number and chunk size as metadata
    5. Ask questions and print the top matches with the page
       they came from

Example document: "Five Steps for Frontier AI Security
Readiness" (CrowdStrike white paper, 2026), included in
this folder.

Setup:

    pip install chromadb pypdf

Run:

    python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf

Ask your own question:

    python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf \\
        --ask "Why is CVSS severity not enough anymore?"

Other options:

    --chunk-size 64       64, 128 or 256 (max 256)
    --rebuild             re-embed even if the stored chunks are unchanged
    --export-text DIR     also save the cleaned text as DIR/<name>.txt,
                          ready for ch07_07_chunking_experiment.py

Run the chunking experiment on this paper:

    python ch07_08_embed_pdf.py CrowdStrike-Frontier-AI-Readiness.pdf \\
        --export-text whitepaper_text
    python ch07_07_chunking_experiment.py \\
        --docs whitepaper_text --questions whitepaper_questions.json
"""

import argparse
import hashlib
import logging
import re
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from pypdf import PdfReader

# pypdf warns that fontTools is missing for some embedded fonts.
# Text extraction is fine without it, so hide the noise.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Reuse the token-aware chunker from Example 7.
from ch07_07_chunking_experiment import (
    MODEL_LIMIT,
    chunk_by_tokens,
    load_model_tokenizer,
    token_count,
)


STORE_PATH = "./ch07_pdf_store"

DEMO_QUESTIONS = [
    "How fast are attackers turning new vulnerabilities into exploits?",
    "What should replace traditional vulnerability management?",
    "What risks come with using AI agents for security?",
]


#
# 1. Extract text, one string per page.
#

def extract_pages(pdf_path: str) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]


#
# 2. Clean.
#
# Every page of this paper starts with a running header like
#   "Five Steps for Frontier AI Security Readiness CROWDSTRIKE7"
# (sometimes glued to the first heading, sometimes with the
# page number in front). Left in, it becomes part of every
# chunk and makes unrelated chunks look similar.
#
# Each PDF has its own boilerplate. Print a few raw pages,
# find what repeats, and write a pattern for it.
#

HEADER = re.compile(
    r"^\s*\d{0,2}\s*(CROWDSTRIKE\s*)?"
    r"Five Steps for Frontier AI Security Readiness"
    r"\s*(CROWDSTRIKE)?\s*\d{0,2}"
)


def clean_page(text: str) -> str:
    text = HEADER.sub("", text, count=1)
    text = re.sub(r"[ \t]+", " ", text)        # runs of spaces
    text = re.sub(r" *\n *", "\n", text)        # spaces around line breaks
    return text.strip()


#
# 3 + 4. Chunk each page and store it.
#
# Chunking page by page means every chunk belongs to exactly
# one page, so answers can cite where they came from. (The
# trade-off: a sentence that runs across a page break gets
# split.)
#

def build_collection(client, ef, tok, pdf_path, pages, chunk_size, rebuild):

    name = f"{Path(pdf_path).stem[:40]}_{chunk_size}"
    name = re.sub(r"[^A-Za-z0-9_-]", "_", name)

    # Chunking is fast; embedding is the slow part. So always chunk,
    # fingerprint the result, and only re-embed when the chunks differ
    # from what is stored (new cleaning or chunking code, a new PDF).
    ids, docs, metas = [], [], []
    for page_no, text in enumerate(pages, start=1):
        if not text:
            continue
        for i, chunk in enumerate(chunk_by_tokens(tok, text, chunk_size, overlap=16)):
            ids.append(f"p{page_no}-c{i}")
            docs.append(chunk)
            metas.append({"source": Path(pdf_path).name,
                          "page": page_no,
                          "chunk_size": chunk_size})

    longest = max(token_count(tok, d) for d in docs)
    assert longest <= chunk_size

    fingerprint = hashlib.sha256(
        "\x00".join(i + "\x01" + d for i, d in zip(ids, docs)).encode("utf-8")
    ).hexdigest()[:16]

    try:
        col = client.get_collection(name, embedding_function=ef)
    except Exception:
        col = None   # doesn't exist yet

    if col is not None and not rebuild and col.count() > 0 \
            and (col.metadata or {}).get("chunks_fingerprint") == fingerprint:
        print(f"Using existing collection '{name}' ({col.count()} chunks, "
              f"unchanged). --rebuild to re-embed anyway.\n")
        return col

    if col is not None:
        reason = "--rebuild" if rebuild else "chunks changed since last run"
        print(f"Re-embedding '{name}' ({reason}).")
        client.delete_collection(name)

    col = client.create_collection(
        name,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine",
                  "embedding_model": "all-MiniLM-L6-v2",
                  "chunk_size": chunk_size,
                  "chunks_fingerprint": fingerprint},
    )
    col.add(ids=ids, documents=docs, metadatas=metas)

    print(f"Embedded {len(docs)} chunks of up to {chunk_size} tokens "
          f"(longest {longest}) into '{name}'.\n")
    return col


#
# 5. Ask.
#

def ask(col, question: str, k: int = 3) -> None:
    res = col.query(query_texts=[question], n_results=k)
    print(f"Q: {question}")
    for doc, meta, dist in zip(res["documents"][0],
                               res["metadatas"][0],
                               res["distances"][0]):
        snippet = " ".join(doc.split())
        print(f"   {1 - dist:.2f}  p.{meta['page']:<3} {snippet[:110]}...")
    print()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Embed a PDF into Chroma and query it.")
    parser.add_argument("pdf")
    parser.add_argument("--chunk-size", type=int, default=128)
    parser.add_argument("--ask", help="a question to ask instead of the demo questions")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--export-text", metavar="DIR")
    args = parser.parse_args()

    if args.chunk_size > MODEL_LIMIT:
        raise SystemExit(f"--chunk-size must be at most {MODEL_LIMIT}.")

    raw = extract_pages(args.pdf)
    pages = [clean_page(p) for p in raw]

    ef = DefaultEmbeddingFunction()
    tok = load_model_tokenizer(ef)

    print(f"{Path(args.pdf).name}: {len(pages)} pages, "
          f"{sum(len(p.split()) for p in pages)} words, "
          f"{sum(token_count(tok, p) for p in pages if p)} tokens")
    print(f"Running header removed from "
          f"{sum(bool(HEADER.match(p)) for p in raw)} pages.\n")

    if args.export_text:
        out = Path(args.export_text)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{Path(args.pdf).stem}.txt"
        path.write_text("\n\n".join(pages), encoding="utf-8")
        print(f"Cleaned text saved to {path}\n")

    client = chromadb.PersistentClient(path=STORE_PATH)
    col = build_collection(client, ef, tok, args.pdf, pages,
                           args.chunk_size, args.rebuild)

    for q in ([args.ask] if args.ask else DEMO_QUESTIONS):
        ask(col, q)
