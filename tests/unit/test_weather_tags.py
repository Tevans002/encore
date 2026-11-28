import pytest
import weather_tags as wt

PROBED = ["ambient", "chillout", "downtempo", "trip hop", "lo-fi", "acoustic", "folk", "singer-songwriter",
          "jazz", "blues", "soul", "classical", "piano", "post-rock", "shoegaze", "dream pop", "synthwave",
          "electronic", "indie rock", "rock", "reggae", "bossa nova"]


def test_table_uses_only_probed_tags():
    assert wt.missing_from_table(PROBED) == []
    assert wt.missing_from_table(PROBED[:-1]) == ["bossa nova"]


@pytest.mark.parametrize("p,c,t,want", [
    ([0, 0.2], [10, 10], [20, 20], "wet"),
    ([0, 0], [70, 70], [20, 20], "overcast"),
    ([0, 0], [69, 69], [4, 4], "cold_clear"),
    ([0, 0], [10, 10], [5, 5], "mild_clear"),
    ([0, 0], [10, 10], [4.99], "cold_clear"),
    ([0], [100], [-20], "overcast"),          # overcast is checked before cold
    ([0.1], [100], [-20], "wet"),             # wet wins over everything
])
def test_buckets(p, c, t, want):
    assert wt.weather_bucket(p, c, t) == want


@pytest.mark.parametrize("p,c,t", [([], [1], [1]), ([0], [], [1]), ([0], [1], []),
                                   ([None], [1], [1]), ([float("nan")], [1], [1]), ([0], ["x"], [1]),
                                   ([True], [1], [1]), (None, [1], [1])])
def test_missing_series_gives_none(p, c, t):
    assert wt.weather_bucket(p, c, t) is None


def test_none_entries_are_skipped_not_fatal():
    assert wt.weather_bucket([0, None], [None, 5], [None, 20]) == "mild_clear"


@pytest.mark.parametrize("h,band", [(0, "night"), (4, "night"), (5, "morning"), (10, "morning"), (11, "afternoon"),
                                    (16, "afternoon"), (17, "evening"), (21, "evening"), (22, "night"), (23, "night")])
def test_hour_band_edges(h, band):
    assert wt.hour_band(h) == band


@pytest.mark.parametrize("h", [-1, 24, 1.5, "9", True, None])
def test_hour_band_rejects_bad(h):
    with pytest.raises(ValueError):
        wt.hour_band(h)


def test_middle_hour():
    assert wt.middle_hour([12, 13, 14, 15, 16, 17]) == 14      # lower middle
    assert wt.middle_hour([12, 13, 14]) == 13
    assert wt.middle_hour([9]) == 9
    assert wt.middle_hour([22, 23, 0, 1, 2]) == 0              # crosses midnight
    for bad in ([], [24], [True]):
        with pytest.raises(ValueError):
            wt.middle_hour(bad)


def test_golden_1_cold_dry_clear_afternoon_puts_acoustic_first():
    b = wt.weather_bucket([0] * 6, [1, 2, 5, 9, 4, 3], [1.3, 2, 3, 4.1, 3.5, 2.5])
    band = wt.hour_band(wt.middle_hour([12, 13, 14, 15, 16, 17]))
    assert (b, band) == ("cold_clear", "afternoon")
    c = wt.candidate_tags(b, band, PROBED)
    assert c[0] == "acoustic" and sorted(c) == sorted(wt.BUCKET_TAGS["cold_clear"])


def test_candidates_are_a_permutation_of_the_bucket_for_every_pair():
    for b in wt.BUCKET_TAGS:
        for band in wt.HOUR_TAGS:
            c = wt.candidate_tags(b, band, PROBED)
            assert sorted(c) == sorted(wt.BUCKET_TAGS[b])
            assert set(c) <= set(PROBED)
            pref = [t for t in c if t in wt.HOUR_TAGS[band]]
            assert c[:len(pref)] == pref                      # preferred tags are in front


def test_no_overlap_keeps_bucket_order():
    assert wt.candidate_tags("overcast", "afternoon", PROBED) == list(wt.BUCKET_TAGS["overcast"])


def test_candidates_reject_unknown_or_out_of_table():
    with pytest.raises(ValueError):
        wt.candidate_tags("sunny", "morning", PROBED)
    with pytest.raises(ValueError):
        wt.candidate_tags("wet", "dawn", PROBED)
    with pytest.raises(ValueError):
        wt.candidate_tags("wet", "morning", [t for t in PROBED if t != "jazz"])


def test_ladder_order():
    out = wt.ladder_order(["jazz", "piano"], PROBED)
    assert out[:2] == ["jazz", "piano"] and sorted(out) == sorted(PROBED)
    for bad in (["jazz", "jazz"], ["nope"]):
        with pytest.raises(ValueError):
            wt.ladder_order(bad, PROBED)
