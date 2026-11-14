"""Position tracking with entry and trailing AVWAPs (section 6)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .avwap import anchored_vwap


def evaluate_position(pos: dict, an) -> dict:
    """pos: row from positions table. an: StockAnalysis built with the entry date as an anchor."""
    df = an.df
    entry = pd.Timestamp(pos["entry_date"])
    close, cy = float(df["close"].iat[-1]), float(df["close"].iat[-2])
    eb = anchored_vwap(df, entry)
    hls_after = [a for a in an.anchors if "higher_low" in a.kinds and a.date > entry]
    latest_hl = hls_after[-1] if hls_after else None
    if pos.get("trailing_anchor_date"):
        t_date, t_label = pd.Timestamp(pos["trailing_anchor_date"]), "Trailing (manual)"
    elif latest_hl is not None:
        t_date, t_label = latest_hl.date, "Trailing (higher low)"
    else:
        t_date, t_label = entry, "Trailing (= entry)"
    tb = anchored_vwap(df, t_date)
    e_now, e_y = eb["avwap"].iat[-1], eb["avwap"].iat[-2]
    t_now, t_y = tb["avwap"].iat[-1], tb["avwap"].iat[-2]
    alerts = []
    if not np.isnan(t_y) and close < t_now and cy >= t_y:
        alerts.append(f"closed below trailing AVWAP {t_now:.2f}")
    if not np.isnan(e_y) and close < e_now and cy >= e_y:
        alerts.append(f"closed below entry AVWAP {e_now:.2f}")
    u2y = tb["u2"].iat[-2]
    if close > tb["u2"].iat[-1] and (np.isnan(u2y) or cy <= u2y):
        alerts.append(f"reached +2σ of trailing AVWAP ({tb['u2'].iat[-1]:.2f})")
    new_hl = None
    if latest_hl is not None and pos.get("last_hl_seen") != latest_hl.key:
        new_hl = latest_hl.key
        alerts.append(f"new higher low on {latest_hl.date:%d %b} — consider moving the trailing anchor up")
    return {
        "id": pos["id"], "symbol": pos["symbol"], "entry_date": f"{entry:%Y-%m-%d}",
        "entry_price": pos["entry_price"], "qty": pos["qty"], "close": close,
        "profit_pct": (close - pos["entry_price"]) / pos["entry_price"] * 100,
        "pnl": (close - pos["entry_price"]) * pos["qty"],
        "entry_avwap": e_now, "dist_entry_pct": (close - e_now) / e_now * 100,
        "trailing_date": f"{t_date:%Y-%m-%d}", "trailing_label": t_label,
        "trailing_avwap": t_now, "dist_trailing_pct": (close - t_now) / t_now * 100,
        "t_u1": tb["u1"].iat[-1], "t_l1": tb["l1"].iat[-1],
        "t_u2": tb["u2"].iat[-1], "t_l2": tb["l2"].iat[-1],
        "alerts": alerts, "new_hl": new_hl,
        "vol_ratio": float(df["volume"].iat[-1] / df["avg_vol20"].iat[-1])
        if df["avg_vol20"].iat[-1] else np.nan,
    }
