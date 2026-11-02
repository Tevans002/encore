"""Helpers shared by both test layers."""

import json
import re

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse(result_json):
    """Decode a tool's return value and check it follows the ToolResult contract."""
    data = json.loads(result_json)
    assert set(data) == {"success", "data", "error", "error_category"}, f"not a ToolResult: {data}"
    if data["success"]:
        assert data["error"] is None and data["error_category"] is None
    else:
        assert data["data"] is None
        assert data["error_category"] in {"transient", "input", "resource", "fatal"}
        assert isinstance(data["error"], str) and data["error"].strip()
    return data
