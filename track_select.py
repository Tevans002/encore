"""Deterministic track selection for Forecast FM (docs/decisions.md, "Track selection rule").

Pure function: no network, no clock, no model. Pick `k` tracks whose total length (whole seconds)
is inside [lo, hi] with at most `cap` tracks per artist, minimising the sum of 0-based radio ranks.
Ties: total closest to `target`, then the lexicographically smallest sorted rank list.
"""
from collections import OrderedDict
from itertools import combinations
from typing import Iterable, Optional

K, CAP = 10, 2
LO_S, HI_S, TARGET_S = 2700, 3300, 3000


def _secs(duration) -> Optional[int]:
    """Track duration in ms -> whole seconds, or None when missing or unusable."""
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return None
    return round(duration / 1000)


def select_tracks(tracks: list, *, k: int = K, cap: int = CAP, lo: int = LO_S, hi: int = HI_S,
                  target: int = TARGET_S, blocked: Iterable[int] = ()) -> dict:
    """Return {"ok", "indices", "total_s", "rank_sum", "reason", "usable", "artists"}.

    `tracks` is the radio list in radio order (dicts with "creator" and "duration" in ms).
    `blocked` holds indices to skip (recently played, failed verification). Ranks stay the
    original indices, so filtering never changes the order preference. On failure `ok` is
    False, `indices` is empty and `reason` is "too_few_usable" or "no_set_in_window".
    """
    skip = set(blocked)
    groups: "OrderedDict[object, list]" = OrderedDict()
    usable = 0
    for i, t in enumerate(tracks):
        s = _secs(t.get("duration"))
        if i in skip or s is None or s > hi:
            continue
        usable += 1
        groups.setdefault(t.get("creator"), []).append((i, s))   # missing creator = one shared artist
    info = {"usable": usable, "artists": len(groups)}
    if usable < k:
        return {"ok": False, "indices": [], "total_s": None, "rank_sum": None,
                "reason": "too_few_usable", **info}

    # state (count, total_s) -> (rank_sum, sorted rank tuple); exact, since extending two partial
    # sets with the same disjoint items keeps their order.
    best = {(0, 0): (0, ())}
    for items in groups.values():
        opts = [c for r in range(1, cap + 1) for c in combinations(items, r)]
        new = dict(best)
        for (n, tot), (rs, rl) in best.items():
            for c in opts:
                n2, tot2 = n + len(c), tot + sum(s for _, s in c)
                if n2 > k or tot2 > hi:
                    continue
                ranks = tuple(sorted(rl + tuple(i for i, _ in c)))
                cand = (rs + sum(ranks) - sum(rl), ranks)
                if (n2, tot2) not in new or cand < new[(n2, tot2)]:
                    new[(n2, tot2)] = cand
        best = new

    finals = [(rs, abs(tot - target), rl, tot) for (n, tot), (rs, rl) in best.items()
              if n == k and lo <= tot <= hi]
    if not finals:
        return {"ok": False, "indices": [], "total_s": None, "rank_sum": None,
                "reason": "no_set_in_window", **info}
    rs, _, rl, tot = min(finals)
    return {"ok": True, "indices": list(rl), "total_s": tot, "rank_sum": rs,
            "reason": "", **info}
