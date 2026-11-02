"""
Chapter 7 — Memory — Example 7
The Agentic AI Bible (Revised & Expanded Edition 2026)

Chunking experiment: does chunk size change retrieval quality?

The embedding model stays fixed (Chroma's default,
all-MiniLM-L6-v2). Only the chunk size changes:
64, 128, and 256 tokens.

Why stop at 256? The model reads at most 256 tokens and
silently ignores the rest. A 512-token chunk would really
be embedded as its first 256 tokens, and the experiment
would measure truncation instead of chunking. Every chunk
here is checked against that limit.

What the script does:

    1. Load documents (a built-in sample help guide, or
       your own .txt / .md files)
    2. Split them into chunks of N tokens, using the SAME
       tokenizer the embedding model uses
    3. Embed each chunk size into its own Chroma collection
    4. Ask every evaluation question against each collection
    5. Report recall@1 and recall@3 for each chunk size

A question counts as a hit at k if one of the top k chunks
contains its answer text.

Setup:

    pip install chromadb

    The first run downloads the embedding model
    (about 80 MB).

Run with the built-in sample:

    python ch07_07_chunking_experiment.py

Run on your own documents:

    python ch07_07_chunking_experiment.py \\
        --docs ./my_docs \\
        --questions my_questions.json

    my_questions.json is a list like:

    [
      {"question": "How do I connect Spotify?",
       "answer": "Settings > Music services"},
      ...
    ]

    "answer" must be text that appears word for word in
    your documents (case does not matter). Keep it short,
    a few words, so it fits inside one chunk.

Other options:

    --sizes 64 128 256     chunk sizes to compare
    --overlap 16           tokens shared by neighboring chunks
    --show-misses          print the questions each size missed
"""

import argparse
import json
import os
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import (
    ONNXMiniLM_L6_V2,
)
from tokenizers import Tokenizer   # installed with chromadb


MODEL_LIMIT = 256   # all-MiniLM-L6-v2 reads at most 256 tokens
SPECIAL = 2         # [CLS] and [SEP] are added to every input


#
# Built-in sample corpus: a made-up help guide for the
# Encore music agent. Replace it with real documents via
# --docs for the real experiment.
#

SAMPLE_DOCS = {
    "encore_help.md": """
# Encore Help Guide

## What Encore is

Encore is a music assistant that works on top of your listening history.
You can ask it what you have been playing, ask for new music, and have it
build or edit playlists for you. Encore remembers earlier conversations,
so you can say things like "same as last time, but more upbeat" and it
will look up what it did before instead of starting from scratch. It
never invents songs: every track it adds must be confirmed to exist in
the music catalog during the same session.

## Connecting a streaming service

Encore can export playlists to Spotify or Apple Music. To connect a
service, open Settings, choose Linked accounts, and sign in to the
service you use. The connection is not permanent. Spotify connections
expire, so you have to reconnect every 180 days; Encore will tell you
when an export fails because the link has lapsed. Apple Music listens are
not reported automatically; install a scrobbler app on your phone so
your plays are recorded.

## How Encore names its playlists

Every playlist Encore creates starts with the [Encore] prefix in its
title, for example "[Encore] Focus - Oct 3". The prefix is how Encore
recognizes its own playlists. Before creating a playlist it searches for
an existing one with the same name, so asking twice updates the first
playlist instead of making a duplicate. Encore can only delete playlists
that carry the prefix. A request to delete all of your playlists removes
only the ones Encore made, and only after you approve.

## Approvals

Some actions change your account, so Encore asks first. Exporting a
playlist and deleting a playlist always require a yes from you. Smaller
changes, such as loving a song you just asked it to love, can go ahead
without a prompt. If you say no, the action is cancelled and Encore
returns a resource error to itself, which means it must not try the same
action again. It will explain what it did not do and suggest an
alternative.

## Radio modes

Encore's radio feature builds a list of candidate tracks from a short
prompt such as a tag or an artist. Radio has three modes. Easy mode
sticks closely to the prompt and favors popular, familiar tracks. Medium
mode mixes familiar tracks with some less obvious ones. Hard mode
reaches for deep cuts the user has never played, so it is the best
choice when you want to be surprised and the worst choice for a dinner
party.

## How a request runs

When you ask for something, Encore works in a loop: it picks a tool,
runs it, reads the result, and decides what to do next. Each request is
limited to at most 12 steps. If it reaches that limit without finishing,
it stops and tells you the step budget was exceeded, along with whatever
partial progress it made. Every request ends with exactly one status:
answered, answered with low confidence, stuck, budget exceeded, or
policy halt.

## When something goes wrong

Errors fall into four groups. Temporary problems, such as a busy server,
are retried automatically with a growing delay between attempts. Input
problems, such as a number out of range, are fixed by correcting the
request. Resource problems, such as a song that does not exist, are
reported rather than retried. An authentication failure is different:
if the service answers with a 401, Encore stops the run immediately and
tells you to check your token, because retrying would never succeed.

## Being polite to the music services

The catalog service is run by a non-profit and asks every client to
limit itself to about one request per second, counted per network
address. Encore waits between catalog lookups to stay under that rate
and caches results that never change, such as the details of a
recording. On a shared campus network all students count as one address,
so avoid running large tests during class.

## What Encore remembers

Encore keeps short summaries of past sessions rather than full
transcripts. A summary records the decisions made, preferences you
stated, and anything left unfinished. Summaries are deleted after 90
days, and Encore keeps at most the 50 most recent summaries for each
person. Before old summaries are removed, any lasting preferences in
them, such as "never play holiday music", are copied into a separate
long-term preference list.

## Deleting your data

You can erase everything Encore has stored about you at any time. The
forget command removes every stored item tied to your user ID: session
summaries, saved preferences, the current session state, and archived
logs. It prints how many items it deleted from each store. It does not
touch your listening history or the playlists in your streaming account.

## Glossary

A B-side is the secondary track released alongside a single, often more
experimental than the main song. Shoegaze is a style of rock built from
layers of distorted, reverb-heavy guitar with buried vocals that blend
into the noise. Focus music is instrumental music with a steady texture
and few surprises, meant to stay in the background while you work. A
deep cut is a lesser-known track from an artist's catalog, rarely played
on the radio.
""",
}

# Questions are paraphrased so they share few words with the
# answer. The answer is the exact text that must appear in a
# retrieved chunk for the question to count as a hit.

SAMPLE_QUESTIONS = [
    {"question": "How often do I need to relink my Spotify account?",
     "answer": "every 180 days"},
    {"question": "How can I tell which playlists the assistant made?",
     "answer": "[Encore] prefix"},
    {"question": "What happens if I refuse to approve an action?",
     "answer": "returns a resource error"},
    {"question": "Is there a cap on how many actions one request can take?",
     "answer": "at most 12 steps"},
    {"question": "Which radio setting finds songs I've never heard?",
     "answer": "deep cuts the user has never played"},
    {"question": "How long are conversation summaries kept?",
     "answer": "deleted after 90"},
    {"question": "What is the maximum number of stored summaries per user?",
     "answer": "50 most recent summaries"},
    {"question": "How do I wipe all of my stored data?",
     "answer": "removes every stored item"},
    {"question": "What does the assistant do when my token is rejected?",
     "answer": "stops the run immediately"},
    {"question": "How fast can the app query the music catalog?",
     "answer": "one request per second"},
    {"question": "What does B-side mean?",
     "answer": "secondary track released alongside a single"},
    {"question": "Describe the shoegaze sound.",
     "answer": "buried vocals"},
    {"question": "Why don't my iPhone Apple Music plays show up?",
     "answer": "install a scrobbler app"},
    {"question": "Will asking for the same playlist twice create two copies?",
     "answer": "updates the first"},
]


#
# Tokenizer: load the exact tokenizer file the embedding
# model uses, WITHOUT its truncation, so we can measure how
# long each chunk really is.
#

def load_model_tokenizer(ef) -> Tokenizer:
    ef(["warm up"])   # makes Chroma download the model if needed
    path = os.path.join(
        ONNXMiniLM_L6_V2.DOWNLOAD_PATH,
        ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME,
        "tokenizer.json",
    )
    tok = Tokenizer.from_file(path)
    tok.no_truncation()
    tok.no_padding()
    return tok


def token_count(tok: Tokenizer, text: str) -> int:
    """Tokens the model will see, including [CLS] and [SEP]."""
    return len(tok.encode(text).ids)


#
# Chunking by tokens.
#
# Tokenize the whole document, take windows of N tokens, and
# cut the ORIGINAL text at those token boundaries (using the
# character offsets the tokenizer reports), so the chunk
# stays readable.
#
# Windows start and end on whole words. A cut in the middle
# of a word ("identif|ying") re-tokenizes into extra pieces
# and can push a chunk past its size; whole words keep the
# count exact.
#

def chunk_by_tokens(
    tok: Tokenizer,
    text: str,
    chunk_tokens: int,
    overlap: int,
) -> list[str]:

    content = chunk_tokens - SPECIAL   # leave room for [CLS] and [SEP]
    if overlap >= content:
        raise ValueError("overlap must be smaller than the chunk size")

    enc = tok.encode(text, add_special_tokens=False)
    offsets, words = enc.offsets, enc.word_ids
    n = len(offsets)

    def mid_word(i: int) -> bool:
        """True if token i continues the word of token i-1."""
        return 0 < i < n and words[i] == words[i - 1]

    def is_punct(i: int) -> bool:
        """True if token i is punctuation only, like '.' or ','."""
        a, b = offsets[i]
        return not any(ch.isalnum() for ch in text[a:b])

    chunks = []
    start = 0
    while start < n:
        end = min(start + content, n)
        while mid_word(end) and end > start + 1:   # back up to a word boundary
            end -= 1

        piece = text[offsets[start][0]:offsets[end - 1][1]].strip()
        # Safety net for a single "word" longer than the window
        # (a long URL, say): trim until the chunk fits.
        while end - start > 1 and token_count(tok, piece) > chunk_tokens:
            end -= 1
            piece = text[offsets[start][0]:offsets[end - 1][1]].strip()

        chunks.append(piece)
        if end >= n:
            break

        nxt = max(end - overlap, start + 1)        # step forward, keep overlap
        while nxt < end and (mid_word(nxt) or is_punct(nxt)):
            nxt += 1                               # start on a whole word, not ". They"
        start = nxt

    return chunks


#
# Load documents and questions
#

def load_docs(folder: str | None) -> dict[str, str]:
    if folder is None:
        return SAMPLE_DOCS
    docs = {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted(Path(folder).glob("*"))
        if p.suffix.lower() in (".txt", ".md")
    }
    if not docs:
        raise SystemExit(f"No .txt or .md files found in {folder}")
    return docs


def load_questions(path: str | None) -> list[dict]:
    if path is None:
        return SAMPLE_QUESTIONS
    return json.loads(Path(path).read_text(encoding="utf-8"))


def norm(text: str) -> str:
    """Lowercase and collapse whitespace, so line breaks don't matter."""
    return " ".join(text.split()).lower()


def check_answers_exist(docs: dict[str, str], questions: list[dict]) -> None:
    """An answer that isn't in the corpus can never be a hit."""
    corpus = norm(" ".join(docs.values()))
    missing = [q["answer"] for q in questions if norm(q["answer"]) not in corpus]
    if missing:
        raise SystemExit(
            "These answers do not appear word for word in the documents:\n  "
            + "\n  ".join(missing)
        )


#
# Experiment
#

def run(sizes: list[int], overlap: int, docs, questions, show_misses: bool):

    too_big = [s for s in sizes if s > MODEL_LIMIT]
    if too_big:
        raise SystemExit(
            f"Chunk sizes {too_big} exceed the model's {MODEL_LIMIT}-token "
            "limit; the extra text would be silently ignored."
        )

    ef = DefaultEmbeddingFunction()
    tok = load_model_tokenizer(ef)

    corpus_tokens = sum(token_count(tok, t) for t in docs.values())
    print(f"Embedding model : all-MiniLM-L6-v2 (max {MODEL_LIMIT} tokens)")
    print(f"Corpus          : {len(docs)} document(s), {corpus_tokens} tokens")
    print(f"Questions       : {len(questions)}")
    print(f"Overlap         : {overlap} tokens\n")

    # In-memory database: each run starts fresh.
    client = chromadb.EphemeralClient()

    rows = []
    for size in sizes:

        # 1. Chunk every document at this size.
        ids, chunks, metas = [], [], []
        for name, text in docs.items():
            for i, chunk in enumerate(chunk_by_tokens(tok, text, size, overlap)):
                ids.append(f"{name}-{i}")
                chunks.append(chunk)
                metas.append({"source": name, "chunk_size": size})

        longest = max(token_count(tok, c) for c in chunks)
        assert longest <= size, "a chunk is longer than its chunk size"

        # 2. One collection per chunk size, so vectors never mix.
        col = client.create_collection(
            f"chunks_{size}",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine",
                      "embedding_model": "all-MiniLM-L6-v2",
                      "chunk_size": size},
        )
        col.add(ids=ids, documents=chunks, metadatas=metas)

        # 3. Ask every question; find the rank of the first chunk
        #    that contains the answer.
        results = col.query(
            query_texts=[q["question"] for q in questions],
            n_results=min(3, len(chunks)),
        )

        hits1 = hits3 = 0
        misses = []
        for q, docs_for_q in zip(questions, results["documents"]):
            ranks = [
                r for r, d in enumerate(docs_for_q, start=1)
                if norm(q["answer"]) in norm(d)
            ]
            if ranks and ranks[0] == 1:
                hits1 += 1
            if ranks:
                hits3 += 1
            else:
                misses.append((q, docs_for_q[0]))

        n = len(questions)
        rows.append((size, len(chunks), longest, hits1, hits3, n))

        if show_misses and misses:
            print(f"--- {size}-token chunks missed {len(misses)} question(s)")
            for q, top in misses:
                print(f"  Q: {q['question']}")
                print(f"     expected: {q['answer']!r}")
                print(f"     top hit : {top[:90]!r}...")
            print()

    # 4. Report.
    print(f"{'chunk size':>10}  {'chunks':>6}  {'longest':>7}  "
          f"{'recall@1':>9}  {'recall@3':>9}")
    for size, count, longest, h1, h3, n in rows:
        print(f"{size:>10}  {count:>6}  {longest:>7}  "
              f"{h1:>3}/{n:<3} {h1 / n:>4.0%}  {h3:>3}/{n:<3} {h3 / n:>4.0%}")

    print("\n'longest' counts [CLS] and [SEP]; it must never exceed "
          f"{MODEL_LIMIT}.")


if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Compare retrieval recall across chunk sizes."
    )
    parser.add_argument("--docs", help="folder of .txt / .md files")
    parser.add_argument("--questions", help="JSON file of questions")
    parser.add_argument("--sizes", type=int, nargs="+", default=[64, 128, 256])
    parser.add_argument("--overlap", type=int, default=16)
    parser.add_argument("--show-misses", action="store_true")
    args = parser.parse_args()

    docs = load_docs(args.docs)
    questions = load_questions(args.questions)
    check_answers_exist(docs, questions)

    run(sorted(args.sizes), args.overlap, docs, questions, args.show_misses)
