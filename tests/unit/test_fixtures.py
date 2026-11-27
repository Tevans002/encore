"""Sanity checks on the recorded, scrubbed fixtures in tests/fixtures/ (no network, no model).

radio_50_tracks.json and forecast_missoula.json come from real responses, scrubbed by
~/scratch/scrub_fixtures.py (kept outside the repo). The two listens files are hand-built to the shape
get_recent_listens reads; they are not real ListenBrainz responses.
"""
import datetime
import json
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import track_select as ts

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = int(datetime.datetime(2026, 11, 24, 14, 15, tzinfo=ZoneInfo("America/Denver")).timestamp())
CUTOFF = NOW - 3 * 86400


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def radio_tracks():
    return load("radio_50_tracks.json")["payload"]["jspf"]["playlist"]["track"]


def mbid(t):
    ident = t["identifier"][0] if isinstance(t["identifier"], list) else t["identifier"]
    return ident.rstrip("/").rsplit("/", 1)[-1]


def test_radio_fixture_shape_and_selection():
    tr = radio_tracks()
    assert len(tr) == 50 and sum(isinstance(t.get("duration"), int) for t in tr) >= 45
    assert len({mbid(t) for t in tr}) == 50
    r = ts.select_tracks(tr)
    assert r["ok"] and len(r["indices"]) == 10 and 2700 <= r["total_s"] <= 3300


def test_artist_cap_binds_when_one_artist_repeats():
    tr = [dict(t) for t in radio_tracks()]
    for t in tr[:6]:
        t["creator"] = "Repeat Artist"
    r = ts.select_tracks(tr)
    assert r["ok"] and Counter(tr[i]["creator"] for i in r["indices"])["Repeat Artist"] <= 2


def test_forecast_matches_the_golden_numbers():
    sc = load("forecast_missoula.json")["structured_content"]
    assert sc["timezone"] == "America/Denver" and sc["utc_offset_seconds"] == -25200
    rows = [h for h in sc["hourly"] if "2026-11-24T12:00" <= h["time"][:16] <= "2026-11-24T17:00"]
    assert len(rows) == 6
    assert round(min(h["temperature_2m"] for h in rows), 1) == 1.3
    assert round(max(h["temperature_2m"] for h in rows), 1) == 4.1
    assert min(h["cloud_cover"] for h in rows) == 1 and max(h["cloud_cover"] for h in rows) == 9
    assert all(h["precipitation"] == 0 for h in rows)


def _listens(name):
    return load(name)["payload"]["listens"]


def _mb(listen):
    return (listen["track_metadata"].get("mbid_mapping") or {}).get("recording_mbid")


def test_listens_covered_file_reaches_back_past_the_cutoff():
    ls = _listens("listens_covered.json")
    assert len(ls) == 100 and min(x["listened_at"] for x in ls) <= CUTOFF
    radio = {mbid(t) for t in radio_tracks()}
    inside = [x for x in ls if x["listened_at"] >= CUTOFF and _mb(x) in radio]
    outside = [x for x in ls if x["listened_at"] < CUTOFF and _mb(x) in radio]
    assert len(inside) == 2 and len(outside) == 1
    assert sum(_mb(x) is None for x in ls) == 1


def test_listens_short_span_file_does_not_reach_the_cutoff():
    ls = _listens("listens_short_span.json")
    assert len(ls) == 100 and min(x["listened_at"] for x in ls) > CUTOFF
    radio = {mbid(t) for t in radio_tracks()}
    assert sum(1 for x in ls if _mb(x) in radio) == 2


def test_fixtures_hold_no_token_text():
    for p in FIX.glob("*.json"):
        assert "token" not in p.read_text(encoding="utf-8").lower(), p.name
