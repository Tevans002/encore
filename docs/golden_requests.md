# Forecast FM: golden requests (written before the feature code) - 2026-11-25

Status: written before any feature code exists (no step machine, workers or weather domain). The MCP client wrapper weather_tool.py already existed and is committed before this file; it is the client, not the feature. None of the goldens has been run. The fixtures below do not exist in the repo yet.

Shared fixtures (to be recorded, with personal data checked first):
- Clock: 2026-11-24 14:15 America/Denver.
- Forecast: the recorded Missoula result. For 12:00 to 17:00 it shows 1.3 to 4.1 C, no precipitation and cloud cover of 1 to 9 percent: cold, dry and mostly clear.
- A recorded 50-track radio result and a recorded listens fixture.
- Trace event names follow Part 7. Some (mcp_call, step_enter) do not exist in the code yet.

## Golden 1: everything up front
Request: "Something for this afternoon in Missoula. 10 tracks, about 50 minutes, nothing I've played in the last 3 days. Save it."
- Fixture detail: at least one track played in the last 3 days also appears in the radio result, so the exclusion has something to catch.
- No question is asked. The user gave the location, the length and the window.
- The weather tool is called before any lb_radio call. Its location comes from the search result's coordinates and timezone.
- One tag-based lb_radio prompt, chosen by a weather table in code. Every tag in the table must be checked live first: a tag the server rejects returns HTTP 400 with a reason, which Patch A now shows to the model.
- The draft has 10 tracks, a total length of 45 to 55 minutes, at most 2 tracks per artist, and none of the recently played tracks.
- All 10 MBIDs pass the format check and then verify_recordings.
- Exactly one [approval needed], for create_playlist, after verification. create_playlist is not offered to the model before that.
- The result is an [Encore]-prefixed playlist. The trace order is weather call, lb_radio, verify_recordings, approval, create_playlist, final.
- Fails if: a write happens before approval, an unverified MBID is written, or the weather call is skipped.

## Golden 2: underspecified
Request: "Make me something for today's weather."
- Fixture detail: no stored location.
- One question only: which city. Nothing else is asked.
- Length and window fall back to the defaults (10 tracks, 50 minutes plus or minus 5, 3 days), and the reply says so.
- No weather or radio call is made before the city is known. Code decides that, not the model.
- After the user answers "Missoula" the run continues as in Golden 1 and never asks again.
- The low-confidence notice on a Missoula search is not treated as an error.

## Golden 3: weather server unavailable
Request: the same as Golden 1, with the weather connection refused (the failure recorded earlier as a ConnectError).
- The weather agent is optional, so the run continues without weather.
- The playlist is built from a neutral default prompt (not yet chosen).
- The number of attempts is bounded. Under Part 3.3 a refused connection is fatal and is not retried; a transient read (429, 503, timeout) is retried once with backoff. Every attempt counts against the shared run budget.
- No forecast is invented. The reply tells the user the weather was unavailable.
- The trace has a failed mcp_call that names the error type.
- The approval gate and every other check still apply.

## Not covered here
- A Reflexion case (for example a misspelled artist that the server rejects with a reason) belongs in the Part 8 set of 10 requests.

## Open
- The weather-to-tag table does not exist. All 22 tags in the two probes returned 50 tracks (see decisions.md). tag:(metal) was seen earlier but is not among those 22.
- The default-prompt choice for Golden 3.
- How the step machine learns the location in Golden 2 (memory is allowed but not decided).
