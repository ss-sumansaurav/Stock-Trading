"""AVWAP clusters (section 4.4)."""
from __future__ import annotations


def find_clusters(values: dict, max_spread_pct: float, close: float | None = None):
    """values: {anchor_key: avwap_value}. Groups AVWAPs whose values are within
    max_spread_pct of each other (measured from the lowest member). Returns a list of
    dicts: low, high, members, mid, position ('overhead'|'support'|'at price')."""
    items = sorted((v, k) for k, v in values.items() if v == v and v is not None)
    clusters, cur = [], []
    for v, k in items:
        if cur and (v - cur[0][0]) / cur[0][0] * 100 > max_spread_pct:
            if len(cur) >= 2:
                clusters.append(cur)
            cur = []
        cur.append((v, k))
    if len(cur) >= 2:
        clusters.append(cur)
    out = []
    for c in clusters:
        lo, hi = c[0][0], c[-1][0]
        d = {"low": lo, "high": hi, "mid": (lo + hi) / 2, "members": [k for _, k in c]}
        if close is not None:
            d["position"] = "overhead" if lo > close else "support" if hi < close else "at price"
            d["dist_pct"] = ((lo if lo > close else hi) - close) / close * 100
        out.append(d)
    return out
