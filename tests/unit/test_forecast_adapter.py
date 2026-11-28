import copy
import json
from pathlib import Path

import pytest

import forecast_adapter as fa

TABLE = ["ambient", "chillout", "downtempo", "trip hop", "lo-fi", "acoustic", "folk", "singer-songwriter", "jazz",
         "blues", "soul", "classical", "piano", "post-rock", "shoegaze", "dream pop", "synthwave", "electronic",
         "indie rock", "rock", "reggae", "bossa nova"]
DATE = "2026-11-24"
HOURS = [12, 13, 14, 15, 16, 17]
REAL = Path(__file__).resolve().parents[1] / "fixtures" / "forecast_missoula.json"


def make(temp=2.0, precip=0.0, cloud=5, unit="°C"):
    rows = [{"time": f"{DATE}T{h:02d}:00", "temperature_2m": temp, "precipitation": precip, "cloud_cover": cloud}
            for h in range(24)]
    return {"is_error": False, "structured_content": {
        "timezone": "America/Denver", "hourly": rows, "truncated": False, "record_count": 24,
        "hourly_units": {"time": "iso8601", "temperature_2m": unit, "precipitation": "mm", "cloud_cover": "%"}}}


def test_golden_1_shape_cold_dry_clear_afternoon():
    s = fa.weather_summary(make(), DATE, HOURS, TABLE)
    assert (s["bucket"], s["band"]) == ("cold_clear", "afternoon")
    assert s["candidates"][0] == "acoustic" and s["hours"] == HOURS


def test_accepts_the_inner_dict_too():
    assert fa.weather_summary(make()["structured_content"], DATE, HOURS, TABLE)["bucket"] == "cold_clear"


def test_wet_and_overcast_and_mild():
    assert fa.weather_summary(make(precip=0.4), DATE, HOURS, TABLE)["bucket"] == "wet"
    assert fa.weather_summary(make(cloud=90), DATE, HOURS, TABLE)["bucket"] == "overcast"
    assert fa.weather_summary(make(temp=18), DATE, HOURS, TABLE)["bucket"] == "mild_clear"


def test_one_rainy_hour_is_not_lost():
    r = make()
    r["structured_content"]["hourly"][14]["precipitation"] = 0.2
    assert fa.weather_summary(r, DATE, HOURS, TABLE)["bucket"] == "wet"


@pytest.mark.parametrize("unit", ["°F", "K", "", None, 5])
def test_non_celsius_is_refused(unit):
    assert fa.weather_summary(make(unit=unit), DATE, HOURS, TABLE) is None


def test_celsius_spellings():
    for u in ("°C", "C", " °c ", "celsius"):
        assert fa.weather_summary(make(unit=u), DATE, HOURS, TABLE) is not None


def test_missing_hour_missing_units_and_error_results_give_none():
    r = make()
    del r["structured_content"]["hourly"][15]
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is None
    r = make(); del r["structured_content"]["hourly_units"]
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is None
    r = make(); r["is_error"] = True
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is None
    for bad in (None, [], "x", {}, {"structured_content": {"hourly": "no"}}):
        assert fa.weather_summary(bad, DATE, HOURS, TABLE) is None


@pytest.mark.parametrize("field", fa.FIELDS)
@pytest.mark.parametrize("bad", [None, "3", True, float("nan"), float("inf")])
def test_a_bad_value_in_a_requested_hour_gives_none(field, bad):
    r = make()
    r["structured_content"]["hourly"][13][field] = bad
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is None


def test_bad_value_outside_requested_hours_is_ignored():
    r = make()
    r["structured_content"]["hourly"][3]["precipitation"] = None
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is not None


def test_other_date_rows_do_not_count():
    assert fa.weather_summary(make(), "2026-11-25", HOURS, TABLE) is None


def test_bad_time_strings_skipped_and_first_row_wins():
    r = make()
    r["structured_content"]["hourly"][12]["time"] = "garbage"
    assert fa.weather_summary(r, DATE, HOURS, TABLE) is None
    r = make()
    r["structured_content"]["hourly"].append({"time": f"{DATE}T12:00", "temperature_2m": 30, "precipitation": 9,
                                              "cloud_cover": 99})
    assert fa.weather_summary(r, DATE, HOURS, TABLE)["bucket"] == "cold_clear"


def test_int_values_are_fine():
    assert fa.weather_summary(make(temp=2, precip=0, cloud=5), DATE, HOURS, TABLE) is not None


def test_argument_errors():
    for hours in ([], [12, 12], [24], [True]):
        with pytest.raises(ValueError):
            fa.forecast_series(make(), DATE, hours)
    for d in ("24-11-2026", "", None):
        with pytest.raises(ValueError):
            fa.forecast_series(make(), d, HOURS)


def test_candidates_stay_inside_the_table():
    with pytest.raises(ValueError):
        fa.weather_summary(make(), DATE, HOURS, [t for t in TABLE if t != "acoustic"])


def test_input_not_mutated():
    r = make(); before = copy.deepcopy(r)
    fa.weather_summary(r, DATE, HOURS, TABLE)
    assert r == before


@pytest.mark.skipif(not REAL.exists(), reason="recorded fixture not found")
def test_recorded_missoula_fixture_gives_golden_1_outcome():
    """Golden 1: 12:00 to 17:00 on 2026-11-24 is cold, dry and mostly clear, so acoustic comes first."""
    real = json.loads(REAL.read_text())
    s = fa.weather_summary(real, DATE, HOURS, TABLE)
    assert s is not None, "the adapter could not read the recorded fixture (time format, unit or a missing hour)"
    assert (s["bucket"], s["band"]) == ("cold_clear", "afternoon")
    assert s["candidates"][0] == "acoustic"
