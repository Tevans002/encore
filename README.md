# Encore — CSCI 391 Midterm

A ListenBrainz music agent that remembers. The full assignment is in `Midterm.html`.

```
encore.py            the starter: complete every TODO (Part 8)
prompts/             procedural memory: system_v1.md (700-word limit) and CHANGELOG.md
traces/              one JSONL trace per session (Part 6)
memory_examples/     runnable examples and guides for every memory store (see its README)
tests/unit/          48 offline tests of the provided tools and helpers
tests/live/          28 tests against the real ListenBrainz and MusicBrainz APIs:
  test_live_apis.py        14  the provided tools, end to end (read-only)
  test_live_endpoints.py   10  every endpoint your Part 3 tools call (read-only)
  test_live_writes.py       4  write round trips on your account (opt-in, restored)
.env.example         copy to .env and fill in
```

## Setup

```
python -m pip install -r requirements.txt
cp .env.example .env            # Windows PowerShell: copy .env.example .env
python -m pytest tests/unit     # should pass before you change anything
```

## Tests

```
python -m pytest tests/unit     # offline: fake network and clock, under a second
python -m pytest tests/live     # real APIs, read-only, about a minute
python -m pytest                # both
```

`test_live_endpoints.py` is executable documentation: each test shows the URL,
parameters, and response shape one of your tools depends on. The LB Radio and
popularity tests need `LISTENBRAINZ_TOKEN`: ListenBrainz refuses anonymous calls
to those endpoints (HTTP 401), so your tools must send the token there too.

### Write round trips (opt-in)

```
ENCORE_WRITE_TESTS=1 python -m pytest tests/live/test_live_writes.py -v
# PowerShell:  $env:ENCORE_WRITE_TESTS=1; python -m pytest tests/live/test_live_writes.py -v
```

These use your token on `LISTENBRAINZ_USER`. They create a private playlist titled
`[Encore test] …`, add and remove a track, and delete it; then love one recording,
clear it, and restore whatever feedback you had before. Cleanup runs even when a
test fails, and leftover `[Encore test]` playlists are deleted on the next run.
If a test prints `CLEANUP FAILED`, fix that item by hand on listenbrainz.org.

### Export: manual check (not automated)

Exporting creates a real playlist in Spotify or Apple Music that no test can
remove, so check it once by hand after `export_playlist` works:

1. In ListenBrainz settings, confirm the service is connected with playlist permission.
2. Ask Encore to make a two-song playlist, then to export it; approve the prompt.
3. Confirm the playlist appears in your Spotify or Apple Music library, then delete it there.
4. Disconnect the service in ListenBrainz and export again: Encore must report a
   `fatal` error that tells you what to fix, and must not retry.

Record the date and result in your README.

You can also open a test file and press VS Code's Run button. The live tests skip
with a reason if the APIs can't be reached. Don't run them during class:
MusicBrainz limits requests per IP address, and the class shares one on campus.

These tests cover the provided code and must keep passing. If you change a
provided function on purpose, update its tests and explain why in this README.
Add tests for the tools you write in the same two layers.

# Encore — Teague Evans

A ListenBrainz music agent that remembers.

## How to run

```bash
python encore.py online            # interactive session (OpenRouter)
python encore.py offline           # interactive session (local LLM)
python encore.py online --forget   # delete all memory for ENCORE_USER_ID
```

Set up .env with:
LISTENBRAINZ_TOKEN
LISTENBRAINZ_TOKEN
LISTENBRAINZ_USER
OPENROUTER_API_KEY, and optionally OPENROUTER_MODEL_CHEAP / OPENROUTER_MODEL_EXPENSIVE
(if the expensive tier is unset, both tiers fall back to the cheap model).

# Architecture

- **Router (Lab 4)**: a cheap-model classifier returns toolset domains
(listening, catalog, discovery, library, memory, control). route()
forces classify_intent, validates with jsonschema, falls back to all domains.
- **ReAct loop**: bounded MAX_STEPS, one tool at a time, critical rules
reinjected on every model call, transient errors retried with backoff_delays(),
write tools gated behind approval, working memory compressed past budget, one
JSONL trace per session.
- **Memory (Part 5)**: hot state (SQLite KV), episodic (Chroma, user-scoped),
semantic (Chroma, shared), procedural (prompts/system_v1.md, versioned).

# Memory design

| Store | Backing | Scope | Holds |
| --- | --- | --- | --- |
| Hot state | SQLite (memory/hot_state.db) | per ENCORE_USER_ID | active playlist, last recording/artist/results, session preferences, tool-call counters |
| Episodic | ChromaDB collection episodic | per ENCORE_USER_ID | session summaries (decisions, preferences, open_threads, key_facts, errors, topics) with timestamp, topics, quality, embedding_model |
| Semantic | ChromaDB collection semantic | shared (not user-scoped) | music glossary, ListenBrainz/MusicBrainz docs |
| Procedural | prompts/system_v1.md | global | role, rules, memory guidance, tool map (at most 700 words, versioned in CHANGELOG.md) |

**Read path (session start)**. Embed the first message, query episodic with
where={"user_id": ENCORE_USER_ID}, re-rank by 0.7·similarity + 0.3·recency
(90-day linear decay), keep the top 3. Front-load into the context in Part 5.1
order: reinjected rules → current objective → key user facts (hot state +
episodic key_facts) → retrieved summaries with dates and the staleness
instruction → conversation. Retrieved summaries arrive inside
<tool_result>-delimited blocks like all tool output.

**Write path (session end)**. A dedicated summarization prompt produces a forced
structured call (six fields). A second cheap-model call scores factual
consistency; flagged summaries are stored with quality=flagged and a
"verify before using" annotation. New summaries are embedded and compared to the
user's existing summaries: cosine ≥ 0.93 merges instead of duplicating.
The write path runs in a finally, so it fires however the session terminates.

**Hot-state updates**. After search_recordings the loop writes last_results
and last_recording; after create_playlist it writes active_playlist; after
get_recent_listens it writes last_recording. These resolve references like
"that song" and "add it to that playlist" in later turns.

# Retention Policy

- **Episodic TTL: 90 days**. **Cap**: 50 most recent summaries per user.
- **Dedup threshold: 0.93**. Rationale: the spec's 0.92–0.95 range balances
merging true near-duplicates (e.g. the same playlist rebuilt) against collapsing
distinct sessions that share a topic. 0.93 is the midpoint: below 0.92 distinct
sessions risk accidental merges, above 0.95 near-duplicates accumulate.
- **apply_retention()** implements the TTL/cap and "summarize then archive standing
preferences" path, but is not automatically invoked on every session start
(see Known limitations).

# Embedding model

**all-MiniLM-L6-v2** (ChromaDB default; downloaded on first run, ~80 MB). Recorded
in every episodic summary's metadata as embedding_model.

# Known limitations

- **Hot-state coverage is partial**. last_recording, last_results, and
active_playlist are written after the relevant tool calls; last_artist and
the tool_calls/step counters are not yet maintained.
- **Retention is not auto-enforced**. apply_retention() exists but is not called
on every session start; the 90-day TTL and 50-summary cap are documented policy,
not a running job.
- **Status-override matrix is partial**. The loop overrides to budget_exceeded
on step limit; it does not yet map every contradiction (e.g. answered-after-fatal,
answered-after-declined-write).
- **Cheap model behaviour**. cohere/north-mini-code:free frequently answers in
prose rather than calling final_answer; the loop degrades to
answered_low_confidence. The summarizer occasionally returns prose instead of
JSON; a fallback stores the prose as a single key_fact rather than losing it.
- **No dedicated tests for the new tools**. The provided 48 unit tests and 10 live
endpoint tests still pass; new tools were verified via live sessions and JSONL
traces, not dedicated unit/live tests.
- **Semantic memory is shared, not user-scoped**. --forget deletes only per-user
stores (hot state, episodic); it does not touch the shared knowledge collection.
- **Encore playlist tracking is two-tiered**. The durable [Encore] title marker
is the authority for delete_playlist; the in-memory ENCORE_CREATED_PLAYLISTS
set is a fast path and does not persist across runs. A playlist created in a
previous session is deletable only because the marker check fetches its title.
- **remove_playlist_items is index-based**. The tool cannot validate index
against playlist length without a read, so out-of-range indices surface as
input errors from the API, prompting a re-read.
- **Feedback enums differ by direction**. set_feedback accepts [1, 0, -1]
(love / clear / hate); get_feedback filters [1, -1] (loved / hated, since
0 means "no feedback" and there is nothing to list). This asymmetry is an API
fact, not an inconsistency.

# Failure mode (WorkBook)



# Traces

One JSONL per session in traces/, one line per event
(route | llm_call | tool_call | approval | memory_read | memory_write | compress | final)
with model, tier, token counts, latency, and tool args/results.

# Export manual check

Date and result of the manual Spotify/Apple Music export check:

- **Date**: [fill in]
- **Result**: [fill in — connected service exported OK; disconnected service produced a fatal error and did not retry]