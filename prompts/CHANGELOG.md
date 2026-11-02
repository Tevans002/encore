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