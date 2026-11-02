"""Retry helpers and known-good test data shared by the live test files."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/, for helpers.py
from helpers import parse

RETRY_DELAYS_S = (2, 5)     # then give up; real APIs are sometimes slow or busy


def http_retry(enc, *args, **kwargs):
    """enc.http() with the retry the agent loop is supposed to do for transient failures."""
    for delay in (*RETRY_DELAYS_S, None):
        status, body, headers = enc.http(*args, **kwargs)
        if enc.classify_status(status) != "transient" or delay is None:
            return status, body, headers
        time.sleep(delay)


def tool_retry(tool, *args, **kwargs):
    """Run a tool, retrying transient failures. Returns the decoded ToolResult."""
    for delay in (*RETRY_DELAYS_S, None):
        result = parse(tool(*args, **kwargs))
        if result["success"] or result["error_category"] != "transient" or delay is None:
            return result
        time.sleep(delay)


_known = {}


def known_recording(enc):
    """A real recording and its artist, looked up once per run: Pearl Jam, "Black"."""
    if not _known:
        status, body, _ = http_retry(enc, "GET", f"{enc.MB_API}/recording",
                                     params={"query": 'recording:"Black" AND artist:"Pearl Jam"', "limit": 5})
        assert status == 200 and body["recordings"], f"MusicBrainz search failed (HTTP {status})"
        rec = next((r for r in body["recordings"] if r["title"].lower() == "black"), body["recordings"][0])
        _known.update(recording_mbid=rec["id"], title=rec["title"],
                      artist_mbid=rec["artist-credit"][0]["artist"]["id"],
                      artist_name=rec["artist-credit"][0]["name"])
    return _known
