"""Turn a recorded or live Open-Meteo forecast result into the series weather_tags needs.

Pure module: no network, no model, no encore import. Never guesses: any gap, bad unit, error result or
missing requested hour gives None, and the driver then treats the weather as failed (default prompt).
Seen in the fixture (key names and types only): structured_content has hourly (list of rows with time,
temperature_2m, precipitation, cloud_cover), hourly_units, timezone, truncated, record_count.
Not seen: the time string format and the unit strings. Both are checked here, not assumed.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Iterable, Optional

import weather_tags as wt

FIELDS = ("temperature_2m", "precipitation", "cloud_cover")
_CELSIUS = {"c", "°c", "celsius", "degc"}


def _body(result: Any) -> Optional[dict]:
    """The structured_content dict, from the whole tool result or from the dict itself. None on an error result."""
    if not isinstance(result, dict):
        return None
    if result.get("is_error"):
        return None
    body = result.get("structured_content", result)
    return body if isinstance(body, dict) and isinstance(body.get("hourly"), list) else None


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v):
        return None
    return float(v)


def _celsius(body: dict) -> bool:
    units = body.get("hourly_units")
    u = units.get("temperature_2m") if isinstance(units, dict) else None
    return isinstance(u, str) and u.strip().lower() in _CELSIUS


def forecast_series(result: Any, date: str, hours: Iterable) -> Optional[dict]:
    """{"hours", "temp", "precip", "cloud"} for exactly the requested local hours of `date` (YYYY-MM-DD).

    None if: error result, no hourly list, temperature not in Celsius, a requested hour is missing, or any of
    the three values for a requested hour is not a real number. Rows come back in the order of `hours`.
    """
    want = [wt._hour(h) for h in hours]
    if not want or len(set(want)) != len(want):
        raise ValueError("hours must be unique ints 0 to 23")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError("date must be YYYY-MM-DD") from None
    body = _body(result)
    if body is None or not _celsius(body):
        return None
    by_hour = {}
    for row in body["hourly"]:
        if not isinstance(row, dict) or not isinstance(row.get("time"), str):
            continue
        try:
            t = datetime.fromisoformat(row["time"])
        except ValueError:
            continue
        if t.date().isoformat() == date and t.hour not in by_hour:
            by_hour[t.hour] = row
    temp, precip, cloud = [], [], []
    for h in want:
        row = by_hour.get(h)
        if row is None:
            return None
        vals = [_num(row.get(f)) for f in FIELDS]
        if any(v is None for v in vals):
            return None
        temp.append(vals[0]); precip.append(vals[1]); cloud.append(vals[2])
    return {"hours": want, "temp": temp, "precip": precip, "cloud": cloud}


def weather_summary(result: Any, date: str, hours: Iterable, table_tags: Iterable) -> Optional[dict]:
    """{"bucket", "band", "candidates", "hours"} or None when the forecast cannot be trusted."""
    hours = list(hours)
    s = forecast_series(result, date, hours)
    if s is None:
        return None
    bucket = wt.weather_bucket(s["precip"], s["cloud"], s["temp"])
    if bucket is None:
        return None
    band = wt.hour_band(wt.middle_hour(hours))
    return {"bucket": bucket, "band": band, "hours": s["hours"],
            "candidates": wt.candidate_tags(bucket, band, table_tags)}
