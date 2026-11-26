"""Track selection: exact, deterministic, window inclusive, artist cap, rank order kept."""
import itertools
import random
from collections import Counter

import pytest

import track_select as ts


def trk(artist, secs):
    return {"creator": artist, "duration": None if secs is None else secs * 1000}


def brute(tracks, k, cap, lo, hi, target, blocked=()):
    ok = [(i, ts._secs(t["duration"])) for i, t in enumerate(tracks)
          if i not in blocked and ts._secs(t["duration"]) is not None]
    best = None
    for combo in itertools.combinations(ok, k):
        if max(Counter(tracks[i]["creator"] for i, _ in combo).values()) > cap:
            continue
        tot = sum(s for _, s in combo)
        if not lo <= tot <= hi:
            continue
        ranks = tuple(sorted(i for i, _ in combo))
        key = (sum(ranks), abs(tot - target), ranks)
        best = key if best is None or key < best else best
    return best


def test_first_ten_win_when_they_fit():
    tracks = [trk(f"a{i}", 300) for i in range(20)]          # 10 x 300 s = 3000 s
    r = ts.select_tracks(tracks)
    assert r["ok"] and r["indices"] == list(range(10)) and r["total_s"] == 3000


def test_window_is_inclusive_on_both_edges():
    low = [trk(f"a{i}", 270) for i in range(10)]             # 2700 s exactly
    high = [trk(f"a{i}", 330) for i in range(10)]            # 3300 s exactly
    assert ts.select_tracks(low)["ok"] and ts.select_tracks(high)["ok"]
    assert ts.select_tracks([trk(f"a{i}", 269) for i in range(10)])["reason"] == "no_set_in_window"
    assert ts.select_tracks([trk(f"a{i}", 331) for i in range(10)])["reason"] == "no_set_in_window"


def test_artist_cap_is_enforced():
    tracks = [trk("same", 300) for _ in range(5)] + [trk(f"a{i}", 300) for i in range(9)]
    r = ts.select_tracks(tracks)
    assert r["ok"]
    assert Counter(tracks[i]["creator"] for i in r["indices"])["same"] == 2


def test_missing_or_bad_durations_are_skipped():
    tracks = [trk("a", None), {"creator": "b", "duration": 0}, {"creator": "c", "duration": "x"},
              {"creator": "d", "duration": True}] + [trk(f"z{i}", 300) for i in range(10)]
    r = ts.select_tracks(tracks)
    assert r["ok"] and r["indices"] == list(range(4, 14))


def test_too_few_usable_and_no_set_report_reasons():
    assert ts.select_tracks([trk("a", 300)] * 3)["reason"] == "too_few_usable"
    assert ts.select_tracks([])["reason"] == "too_few_usable"
    only_two_artists = [trk("a", 300)] * 6 + [trk("b", 300)] * 6   # cap 2 -> at most 4 tracks
    r = ts.select_tracks(only_two_artists)
    assert r["reason"] == "no_set_in_window"
    assert not r["ok"] and r["indices"] == []


def test_blocked_indices_keep_original_ranks():
    tracks = [trk(f"a{i}", 300) for i in range(14)]
    r = ts.select_tracks(tracks, blocked={0, 3})
    assert r["indices"] == [1, 2, 4, 5, 6, 7, 8, 9, 10, 11]


def test_missing_creator_counts_as_one_artist():
    tracks = [{"creator": None, "duration": 300000} for _ in range(5)] + [trk(f"a{i}", 300) for i in range(9)]
    r = ts.select_tracks(tracks)
    assert sum(1 for i in r["indices"] if tracks[i]["creator"] is None) == 2


def test_same_input_same_output():
    rnd = random.Random(5)
    tracks = [trk(f"a{rnd.randint(0, 25)}", rnd.randint(150, 420)) for _ in range(50)]
    assert ts.select_tracks(tracks) == ts.select_tracks(list(tracks))


@pytest.mark.parametrize("seed", range(60))
def test_matches_brute_force_on_small_pools(seed):
    rnd = random.Random(seed)
    n = rnd.randint(5, 11)
    tracks = [trk(rnd.choice("abcd"), rnd.choice([None, rnd.randint(100, 400)])) for _ in range(n)]
    blocked = {rnd.randrange(n)} if rnd.random() < 0.4 else set()
    k, cap, lo, hi, target = 3, 2, 500, 800, 650
    want = brute(tracks, k, cap, lo, hi, target, blocked)
    got = ts.select_tracks(tracks, k=k, cap=cap, lo=lo, hi=hi, target=target, blocked=blocked)
    if want is None:
        assert not got["ok"]
    else:
        assert got["ok"] and (got["rank_sum"], abs(got["total_s"] - target), tuple(got["indices"])) == want


def test_realistic_pool_runs_fast_and_lands_in_window():
    rnd = random.Random(9)
    tracks = [trk(f"x{i % 40}", rnd.randint(140, 400)) for i in range(50)]
    r = ts.select_tracks(tracks)
    assert r["ok"] and 2700 <= r["total_s"] <= 3300 and len(r["indices"]) == 10
