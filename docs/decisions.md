# Forecast FM: decision log

Each entry is dated and separates what was seen from what was inferred. Evidence comes from
prompts/CHANGELOG.md, the saved midterm traces (traces/ is git-ignored) and the live probes run so far.

## Energy arc dropped - 2026-11-25
- Planned: the Forecast FM spec says the forecast and local time "shape the mood and energy arc", so the plan was to score drafts on an energy arc.
- Found: none of the 19 midterm tools returns energy, valence or tempo. Radio tracks carry only album, creator, duration, extension, identifier and title. MusicBrainz tags are the only per-track signal; they cost one lookup per track at a 1.5 s throttle. For one tag:(ambient) track the top tags were electronic, progressive trance and trance (count 3) and ambient was 1 (n = 1).
- Changed to: code scores each draft on length (10 tracks, 50 minutes plus or minus 5), artist cap (at most 2 per artist), excluded window (3 days; "unknown" if the last 100 listens do not reach back that far) and verified MBIDs. The weather-to-mood link is carried only by the choice of radio prompt, and no code checks it.
- Cost of not having it: code cannot tell a calm playlist from a loud one, and a wrong weather-to-tag mapping would pass every check. Part 8 should report this.

## Part 2 planning: Reflexion (2B), not Tree-of-Thoughts - 2026-11-25
- Earlier plan: Tree-of-Thoughts with a deterministic scorer and code-built candidates. No ToT code was written.
- Evidence from the 18 midterm traces: 16 of 62 tool calls failed (13 of 14 lb_radio, 3 of 3 set_feedback), all category input. Of the 16, 11 were followed only by more failures of the same tool, 4 had no later call to that tool, and 1 was followed by a success with changed arguments.
- Why ToT looked weak here (expectation, not a measurement): one tag:(ambient) radio call returned 50 tracks from 50 distinct artists, so the artist cap and the length window would likely pass at the first candidate and the tree would stop immediately. One artist:(Pearl Jam) call returned 36 tracks from 20 artists, so the cap can bind there.
- Chosen: Reflexion. Three cheaper fixes were made first and are logged in prompts/CHANGELOG.md: the server's failure reason is shown to the model, malformed MBIDs are rejected locally, and a failed playlist lookup no longer reads as "no such playlist". Part 1 has to argue what Reflexion adds beyond them, and Part 8 should compare before and after on the same prompts.
- Where Reflexion is expected to help (not yet tested): failures that code cannot prevent, such as a misspelled artist. The server answered those with HTTP 400 and a reason in the live probe.

## Parts 4 and 5: step machine with a supervisor and workers - 2026-11-25
- Part 4: step machine. Code decides when a step is complete and when to advance. Each state exposes only its own tools.
- Part 5: 5A supervisor-worker. The supervisor calls no tools. The LLM supervisor decides which worker to dispatch and what to ask, within the current state; code decides completion, advancement and escalation.
- Justification category: specialization. This is a hypothesis, not a result. The test is the same requests run against a flat agent with every tool and against focused workers, comparing wrong-tool calls, total tool calls and tokens from the traces. If the flat agent does no worse, the workbook says so.
- One worker owns both approval requests and episodic-memory writes (to be named in the README diagram).
- Part 6 (extra credit) is skipped for now.
- The instructor expects the design to change as the evaluation exposes problems, so changes are logged here with their reasons.

## Time source: zoneinfo - 2026-11-25
- Question asked: "Is the 'time server' required for Forecast FM? Python's zoneinfo may cover local time without the need for the server."
- Reply received (quoted as written): "use whatever provider you wish".
- Decision: local time comes from Python's zoneinfo, with the clock behind one injectable function so tests can pin it. The zone name comes from the Open-Meteo search result or forecast.
- Seen: zoneinfo on the Spark gave 14:15 at -07:00 for America/Denver, the same as the forecast response's utc_offset_seconds (-25200) and the server's current-conditions time (2026-11-24T14:15).
- Risk: if the grading checklist expects a second MCP server, this loses that point. The clock stays behind one function so a time-server call could replace it.

## Playlist target and excluded window - 2026-11-25
- Target: 10 tracks, 50 minutes plus or minus 5. The first 10 tracks of one radio call totalled about 53 minutes (one sample).
- Excluded window: 3 days, checked against the last 100 listens. If the oldest listen fetched is newer than 3 days ago the check returns "unknown". Proposed handling of "unknown", not yet confirmed: continue, log it and tell the user.
- When the weather server is unavailable the run builds from a neutral default prompt, as in the Part 5.1 fault-tolerance example (Golden 3). That prompt is not yet chosen and must be checked live first.

## Baseline setup - 2026-11-25
- All 77 model calls in the 18 midterm traces used local Qwen models (tier "cheap"), so the cheap-to-expensive handoff was not exercised. Cost comparisons for Part 5 use this single-local-model setup on both sides.
- The router's model call does not appear as an llm_call event in the traces (the route events carry no token counts; get_available_tools has not been read), so per-session model-call and token counts probably leave it out. Both sides of any comparison must count it the same way.
