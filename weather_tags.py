"""Weather bucket, hour band and candidate tags for Forecast FM.

Pure module: no network, no model, no encore import. The table below is a DRAFT of mood guesses
(docs/decisions.md, "Personalisation and the weather-to-tag table"): not approved, not validated against real
music. The thresholds (70 percent cloud, 5 C, the hour edges) are guesses too.
Code picks the candidate tags; the model may only reorder them (see tag_order.py).
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

CLOUD_OVERCAST_PCT = 70
COLD_BELOW_C = 5.0

BUCKET_TAGS = {
    "wet": ("lo-fi", "jazz", "piano", "trip hop", "ambient"),
    "overcast": ("downtempo", "shoegaze", "dream pop", "singer-songwriter", "post-rock"),
    "cold_clear": ("acoustic", "folk", "classical", "piano", "ambient"),
    "mild_clear": ("indie rock", "bossa nova", "reggae", "soul", "electronic"),
}
HOUR_TAGS = {
    "morning": ("acoustic", "folk", "singer-songwriter", "bossa nova", "piano"),
    "afternoon": ("acoustic", "indie rock", "soul", "jazz", "chillout"),
    "evening": ("downtempo", "jazz", "blues", "soul", "dream pop"),
    "night": ("ambient", "chillout", "lo-fi", "piano", "classical", "trip hop", "synthwave"),
}


def _nums(values) -> list:
    out = []
    for v in values or ():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if math.isnan(v) or math.isinf(v):
            continue
        out.append(float(v))
    return out


def weather_bucket(precip: Iterable, cloud: Iterable, temp: Iterable) -> Optional[str]:
    """wet (any precipitation), overcast (dry, mean cloud >= 70), cold_clear (dry, mean temp < 5), else mild_clear.

    Returns None when any of the three series has no usable number: never guess a forecast.
    """
    p, c, t = _nums(precip), _nums(cloud), _nums(temp)
    if not p or not c or not t:
        return None
    if any(x > 0 for x in p):
        return "wet"
    if sum(c) / len(c) >= CLOUD_OVERCAST_PCT:
        return "overcast"
    if sum(t) / len(t) < COLD_BELOW_C:
        return "cold_clear"
    return "mild_clear"


def _hour(h) -> int:
    if isinstance(h, bool) or not isinstance(h, int) or not 0 <= h <= 23:
        raise ValueError(f"hour must be an int 0 to 23; got {h!r}")
    return h


def hour_band(hour: int) -> str:
    """morning 05-11, afternoon 11-17, evening 17-22, night 22-05 (local hour, start inclusive)."""
    h = _hour(hour)
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


def middle_hour(hours: list) -> int:
    """The middle entry of the requested local hours, in the order given (lower middle for an even count).

    Taking the entry, not an average, keeps a request that crosses midnight correct.
    """
    if not hours:
        raise ValueError("hours must not be empty")
    hs = [_hour(h) for h in hours]
    return hs[(len(hs) - 1) // 2]


def missing_from_table(table_tags: Iterable) -> list:
    """Tags this module uses that are not in the live-checked table (sorted). Empty means no drift."""
    have = set(table_tags)
    used = {t for ts in BUCKET_TAGS.values() for t in ts} | {t for ts in HOUR_TAGS.values() for t in ts}
    return sorted(used - have)


def candidate_tags(bucket: str, band: str, table_tags: Iterable) -> list:
    """The bucket's tags, those the hour band prefers first, otherwise in table order. Always in table_tags."""
    if bucket not in BUCKET_TAGS:
        raise ValueError(f"unknown bucket {bucket!r}")
    if band not in HOUR_TAGS:
        raise ValueError(f"unknown hour band {band!r}")
    table = set(table_tags)
    base = BUCKET_TAGS[bucket]
    bad = [t for t in base if t not in table]
    if bad:
        raise ValueError(f"bucket tags not in the live-checked table: {bad}")
    pref = HOUR_TAGS[band]
    return [t for t in base if t in pref] + [t for t in base if t not in pref]


def ladder_order(order: list, table_tags: Iterable) -> list:
    """`order` first, then the rest of the table in table order, for the escalation ladder."""
    table = list(table_tags)
    if len(set(order)) != len(order) or any(t not in table for t in order):
        raise ValueError("order must be unique tags from the table")
    return list(order) + [t for t in table if t not in order]
