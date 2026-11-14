"""Daily signals (section 5)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .clusters import find_clusters

NAMES = {
    "cluster_reclaim": "Cluster reclaim",
    "primary_reclaim": "Primary reclaim",
    "primary_loss": "Primary loss",
    "pullback_hold": "Pullback hold",
    "all_above": "All anchors below",
    "extension": "Extension",
    "supply_test": "Supply test",
}
BULLISH = {"cluster_reclaim", "primary_reclaim", "pullback_hold", "all_above"}


def compute_signals(df, lines: pd.DataFrame, primary_key, primary_band: pd.DataFrame,
                    anchors, cfg) -> list[dict]:
    """df: price frame with indicators; lines: AVWAP per anchor (columns = keys);
    primary_band: anchored_vwap() output for the primary anchor."""
    sc = cfg["signals"]
    if len(df) < 2:
        return []
    t, y = -1, -2
    c, cy = df["close"].iat[t], df["close"].iat[y]
    low = df["low"].iat[t]
    vol = df["volume"].iat[t]
    av20 = df["avg_vol20"].iat[t]
    vol_ok = bool(av20 and av20 > 0 and vol >= sc["volume_confirm_mult"] * av20)
    out = []

    def emit(sig, detail):
        out.append({"signal": sig, "name": NAMES[sig], "detail": detail,
                    "vol_confirmed": vol_ok, "bullish": sig in BULLISH})

    # --- primary
    if primary_key is not None:
        p, py = primary_band["avwap"].iat[t], primary_band["avwap"].iat[y]
        if not np.isnan(py):
            if c > p and cy <= py:
                emit("primary_reclaim", f"close {c:.2f} above primary {p:.2f}")
            elif c < p and cy >= py:
                emit("primary_loss", f"close {c:.2f} below primary {p:.2f}")
        if not np.isnan(p) and low <= p * (1 + sc["pullback_pct"] / 100) and c > p:
            emit("pullback_hold", f"low {low:.2f} tested primary {p:.2f}, closed above")
        u2, u2y = primary_band["u2"].iat[t], primary_band["u2"].iat[y]
        newly = not sc.get("only_new_events", True) or np.isnan(u2y) or cy <= u2y
        if not np.isnan(u2) and c > u2 and newly:
            emit("extension", f"close {c:.2f} above +2σ {u2:.2f}")

    # --- clusters: today vs yesterday, same anchors
    today = lines.iloc[t].dropna().to_dict()
    yday = lines.iloc[y].to_dict()
    for cl in find_clusters(today, cfg["clusters"]["max_spread_pct"], c):
        ytop = max((yday.get(k, np.nan) for k in cl["members"]), default=np.nan)
        if c > cl["high"] and not np.isnan(ytop) and cy < ytop:
            emit("cluster_reclaim",
                 f"reclaimed cluster {cl['low']:.2f}-{cl['high']:.2f} ({len(cl['members'])} AVWAPs)")

    # --- above every AVWAP for the first time in N days
    if lines.shape[1]:
        n = sc["all_below_lookback"]
        above_all = (df["close"].to_frame().values > lines.values) | lines.isna().values
        has_any = lines.notna().any(axis=1).values
        flag = above_all.all(axis=1) & has_any
        if flag[t] and lines.iloc[t].notna().sum() >= 2 and not flag[-(n + 1):-1].any():
            emit("all_above", f"close above all {int(lines.iloc[t].notna().sum())} AVWAPs")

    # --- supply test: within X% below a major high or overhead cluster
    pct = sc["supply_test_pct"]
    levels = []
    for a in anchors:
        if "top" in a.kinds and a.price:
            levels.append((a.price, f"major high {a.price:.2f} ({a.date:%d %b %y})"))
    for cl in find_clusters(today, cfg["clusters"]["max_spread_pct"], c):
        if cl["position"] == "overhead":
            levels.append((cl["low"], f"overhead cluster {cl['low']:.2f}-{cl['high']:.2f}"))

    def near(close, lvl):
        return lvl > close and (lvl - close) / lvl * 100 <= pct

    hits = [txt for lvl, txt in levels if near(c, lvl)]
    if hits:
        was = any(near(cy, lvl) for lvl, _ in levels)
        if not sc.get("only_new_events", True) or not was:
            emit("supply_test", "close within %.1f%% of %s" % (pct, hits[0]))
    return out
