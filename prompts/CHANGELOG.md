# System prompt changelog

## v1
- Initial version.

# Procedural Memory Changelog

## v1 — 2026-10-31
- Filled Role, Rules, Memory, Tools TODOs (Part 5.2).
- Added critical rule: never store/log the token (Part 5.2).
- No few-shot examples added; none changed behavior so far.
- added the token rule and aligned the error/verify wording.

## v2 — 2026-11-01
- testing surfaced that the cheap model answered in prose; added final_answer rule.
- Rules: tool results are untrusted data (delimited with <tool_result>); loop wraps tool output.
## v3 — 2026-11-04
- Rules line 12 (delete_playlist/export_playlist approval): added that the script shows the approval prompt itself, so the model should call the tool directly and not ask for confirmation in prose.
- Reason: on the 4B (qwen3-4b-16k), "delete my most recent playlist" twice produced prose ("I will request confirmation...") with no delete_playlist call, so the approval gate was never reached.
- Tested on the 4B only; model unchanged. Word count 484 -> 507.
- Result: no improvement. The 4B still wrote the approval question as prose ("Approval needed: ... (yes/no)") and made no delete_playlist call. Playlist untouched. Next: test the same prompt on the 30B before changing any more wording.

## v4 — 2026-11-04
- Role (lines 7-8): replaced "you confirm before destructive actions" with "you act on clear requests by calling tools, the script asks the user to approve writes for you".
- Reason: v3 retests on the 4B (qwen3-4b-16k) still produced prose ("Please confirm if you would like me to proceed") with no delete_playlist call, even with a clean memory store. The Role line conflicted with the line 12 rule added in v3.
- Tested on the 4B only; model unchanged. Word count 507 -> 520.
- Result: no effect on the model. Only the "# Rules" block is sent to the model (_extract_rules_block); the Role section (lines 7-8) is never sent, so this edit was not a real test. The 4B output matched v3. Reverted: system_v1.md restored to the v3 text.

## Model test — 2026-11-05
- Same prompt (v3 Rules, 507 words), same code, clean memory store; only the model changed: qwen3-4b-16k -> qwen3-30b-16k (qwen3:30b-a3b-instruct-2507-q4_K_M, num_ctx 16384).
- Result: the 30B called find_playlists, then delete_playlist; the [approval needed] gate appeared and the delete went through after approval. The 4B never made a delete_playlist call in 5 runs.
- Conclusion: the failure was the 4B's tool-calling, not the router, loop, rules extraction, or memory. Not yet tested: whether the original v1 prompt also works on the 30B, or how reliable the 30B is over repeated runs.
- Known issue: the summarizer recorded the unexecuted delete request as a "decision" in the 4B runs.

## Router + schema fix (code, no prompt text change) — 2026-11-05
- get_available_tools (encore.py ~line 711): control is now always added to the routed domains, so final_answer is always offered. Before, final_answer was only offered if the router happened to return control (ROUTER_PROMPT does not require it).
- Reason: the listens run and the playlist runs wrote final_answer(...) as prose; in playlist run 1 the router returned ['library', 'discovery'] and in run 2 ['library'], both with no control, so the model had no final_answer tool in those runs. The 4B listens run's routing was not recorded; ['listening'] with no control was seen only in standalone 30B classifier calls.
- Tested with classifier-only calls (3 prompts x 3 runs): control present in all 9, other domains unchanged, delete request shows control once. Full agent run NOT yet done, so it is not known whether the model now calls final_answer.
- lb_radio schema description (encore.py ~line 922): added that for a genre the prompt should be 'tag:(metal)' and that 'genre:' is not valid. Reason: lb_radio {"prompt": "genre:metal"} returns HTTP 400 (error_category input) when called directly; tag:(metal) and tag:metal succeed. Run 1 hit this 5 times and ended budget_exceeded at MAX_STEPS 12.
- lb_radio edit untested in the agent: in the next run the router returned ['library'] only, so the model never had lb_radio.
- Not explained yet: the same playlist prompt routed to ['library'] in a real run but to ['discovery', 'library'] 3/3 in standalone calls.

## Router prompt sentence (ROUTER_PROMPT, encore.py line 116) — 2026-11-05
- Present in the working tree, uncommitted: "Building a playlist from a genre, mood, or artist needs discovery (to find tracks), catalog (to verify recordings), and library (to create the playlist)."
- When it was added is not recorded, so before/after router behavior was never compared.
- Observed with it present (30B, classifier-only, 3 runs): "create a metal music playlist" -> ['discovery', 'library'] 3/3, no catalog. So it does not make the router add catalog.
- Possible link to the earlier ['library']-only routing in run 2: unconfirmed.

## Playlist run after control fix — 2026-11-05
- 30B (qwen3-30b-16k), code with control always routed, clean memory store (--forget first), prompt "create a metal music playlist".
- Result (1 run): one [approval needed] for create_playlist with 50 recording_mbids; approved; user confirmed in ListenBrainz that the playlist exists with 50 tracks. No invented-MBID set_feedback loop this time.
- Not known from this run: whether lb_radio was called or with what prompt (so the lb_radio schema edit is still untested), whether it ended via final_answer or the prose fallback, and whether the tracks are metal.
- One run only; not rehearsed.

## Weather client wrapper (weather_tool.py) — 2026-11-24
- Added weather_tool.py: call_tool(name, args) runs one call against the hosted Open-Meteo MCP server (mcp 2.3.0) and returns structured_content, or raises WeatherError. Not wired into encore.py yet: no change to the prompt, router, TOOL_SCHEMAS or Rules.
- Server text, including the low-confidence geocoding notice, is dropped, not returned (third-party text).
- Tested offline: 11 tests in tests/unit/test_weather_tool.py; 59 unit tests pass. Tested live in standalone scripts only: good forecast, bad variable, latitude 100, no variable lists, 1 ms timeout, connection refused, DNS failure; a good call after the failures still worked. Calls took 0.4-1.8 s.
- Not tested: running inside encore.py's process after open_client(); HTTP 5xx; a server that connects and then hangs; a DST change.

## Failure reasons passed through (lb_radio, create_playlist) — 2026-11-25
- Cause: in the 18 midterm traces, 13 of 14 lb_radio calls and 3 of 3 set_feedback calls failed with category input. For lb_radio the tool result carried only the HTTP status, so the server's reason never reached the model (the 400 reasons were reproduced live for genre: and a connector word, not for every failed prompt). set_feedback's 3 failures are explained in the next entry. Of the 10 lb_radio calls that followed a failed one, 9 failed again.
- Changed: lb_radio and create_playlist failures now end with "Server said: <reason>" (one line, at most 300 characters, ListenBrainz token replaced by ***); the HTTP status stays in the message. tool_call trace events gain an error field (at most 300 characters; None on success). The text is third-party data and is still wrapped as untrusted.
- Checked: 13 new unit tests (72 pass). Three live lb_radio calls: genre: and a connector word returned the server's reasons (genre: lists the valid entities), no token in the result, tag: returned 50 tracks.
- Prediction, not yet tested: with the reason visible, repeated failed lb_radio calls in a session should drop. Part 8 should compare the same prompts before and after.
- Not changed in this entry: set_feedback and the other write tools (covered by the next entry), and the trace error field, which has not been seen in a real session trace.

## Local MBID format checks on write tools — 2026-11-25
- Cause: the 3 failed set_feedback calls in the traces were all in one session, were approved at the prompt, and carried the same 36-character recording_mbid with correct dash positions but 8 non-hex characters (not a UUID). The status code and server message were not recorded, so that the server rejected them for the malformed MBID is inferred, not seen. No session called verify_recordings before a write.
- Changed: set_feedback, create_playlist and add_playlist_items now reject a recording MBID that is not 36 characters of 8-4-4-4-12 hex digits with an input error before any network call, naming the bad field (or list index) and telling the model not to invent MBIDs. set_feedback, add_playlist_items and the final delete request in delete_playlist now pass the server's reason through, as in the previous entry.
- Checked: 28 new unit tests (100 pass). The new check rejects all 3 recorded values; the check makes no network call (the test script replaced http with a function that raises).
- Limits: this is a format check only. It cannot catch a well-formed MBID that does not exist; verify_recordings does that, and code still does not require it before a write. remove_playlist_items and playlist_mbid arguments are not checked. A non-string recording_mbid reaches .strip() in set_feedback first and raises AttributeError before this check; nothing in the dispatch loop catches tool exceptions.
- Prediction, not yet tested: malformed-MBID write failures go to zero in the Part 8 prompts. The failures that remain would be well-formed but unknown MBIDs.

## Fail-closed playlist lookup (create_playlist) — 2026-11-25
- Cause: by reading the code (not seen in a trace), _encore_playlists_with_title returned an empty list on any non-200 reply, and create_playlist read an empty list as "no Encore playlist with this title" and created one. A lookup that failed (429, 5xx, timeout) after a playlist already existed would therefore have produced a second playlist with the same title, for example on a retry or a resumed run.
- Changed: a failed lookup now returns its failure ("ListenBrainz playlist lookup failed (HTTP N)." plus the server's reason) and create_playlist returns that failure without creating or adding anything. The category comes from classify_status: transient for 429, 5xx and network errors, fatal for 401. By the dispatch code read earlier, transient results are retried by the loop with backoff (4 attempts), so a flaky lookup now retries instead of creating; not yet run.
- Checked: 8 new unit tests (108 pass). In a sandbox, 5 of the 8 failed against a stand-in of the old behaviour and all 8 passed after the change. Not run against the real ListenBrainz API.
- Limits: if a create request times out after ListenBrainz made the playlist, the retry's lookup finds it and add_playlist_items posts the same tracks again; whether ListenBrainz ignores duplicate tracks has not been checked. The lookup still fetches only 100 playlists. A 200 reply that is not JSON still raises inside the lookup.
- Prediction, not yet tested: in the Part 8 resume test, forcing the lookup to fail (the --inject fault injection applies to ListenBrainz URLs) and then resuming leaves exactly one playlist with that title.
