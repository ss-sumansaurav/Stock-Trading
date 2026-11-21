"""The "4-AVWAP" setup: is price above every AVWAP that matters over the last year?

Four anchors per stock (all thresholds in settings.yaml, section four_avwap):
  1. 1Y high   - day of the highest high in the last 252 trading days (sellers' reference)
  2. 1Y low    - day of the lowest low in the same window (buyers' reference)
  3. Results   - most recent quarterly results; anchored on the first trading day AFTER the
                 results date, because Indian companies mostly announce after market hours
                 and the market reacts the next session
  4. Volume    - the highest-volume day in the window whose volume is still at least 10% of
                 all volume traded from that day to today. If the biggest day fails (it is
                 old and diluted), the next-biggest qualifying day is used.

The test: close above the HIGHEST of the four AVWAPs, i.e. above all of them. Every group of
holders (top buyers, bottom buyers, results reaction, big-volume players) is then in profit,
so there is no overhead supply from any of them.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .avwap import anchored_vwap

KEYS = ("high", "low", "results", "volume")
LABELS = {"high": "1Y high", "low": "1Y low", "results": "Results", "volume": "Volume day"}


def volume_anchor(df: pd.DataFrame, start: int, min_share_pct: float):
    """Index of the biggest-volume day (from `start`) whose volume >= min_share% of the volume
    from that day to the last day. Returns (index, share_pct) or (None, None)."""
    vol = df["volume"].to_numpy(dtype=float)
    tail_sum = np.cumsum(vol[::-1])[::-1]  # volume from day i to the end, inclusive
    order = np.argsort(-vol[start:], kind="stable") + start
    for i in order:
        if tail_sum[i] > 0:
            share = vol[i] / tail_sum[i] * 100
            if share >= min_share_pct:
                return int(i), float(share)
    return None, None


def results_anchor(index: pd.DatetimeIndex, results_dates, next_day: bool = True):
    """Anchor index for the most recent results date on or before the last bar."""
    if not len(results_dates):
        return None, None
    last = index[-1]
    ds = sorted(pd.Timestamp(d) for d in results_dates if pd.Timestamp(d) <= last)
    if not ds:
        return None, None
    d = ds[-1]
    pos = index.searchsorted(d, side="right" if next_day else "left")
    if pos >= len(index):
        return None, d  # results came out today after the close: anchor tomorrow
    return int(pos), d


def four_avwap(df: pd.DataFrame, cfg: dict, results_dates=()) -> dict:
    fc = cfg.get("four_avwap", {})
    lookback = fc.get("lookback_days", 252)
    share_min = fc.get("volume_share_min_pct", 10.0)
    near_pct = fc.get("near_pct", 2.0)
    n = len(df)
    start = max(0, n - lookback)
    win = df.iloc[start:]
    idx = {
        "high": start + int(np.argmax(win["high"].to_numpy())),
        "low": start + int(np.argmin(win["low"].to_numpy())),
    }
    vi, share = volume_anchor(df, start, share_min)
    idx["volume"] = vi
    ri, rdate = results_anchor(df.index, results_dates, fc.get("results_next_day", True))
    idx["results"] = ri

    close, prev_close = float(df["close"].iat[-1]), float(df["close"].iat[-2]) if n > 1 else np.nan
    out = {"anchors": {}, "missing": []}
    today_vals, yday_vals = [], []
    for k in KEYS:
        i = idx[k]
        if i is None:
            out["missing"].append(k)
            continue
        line = anchored_vwap(df, df.index[i])["avwap"]
        v, vy = float(line.iat[-1]), float(line.iat[-2]) if n > 1 else np.nan
        a = {"date": f"{df.index[i]:%Y-%m-%d}", "avwap": v,
             "dist_pct": (close - v) / v * 100}
        if k == "volume":
            a["volume_share_pct"] = share
        if k == "results":
            a["results_date"] = f"{rdate:%Y-%m-%d}"
        out["anchors"][k] = a
        today_vals.append(v)
        if vy == vy:
            yday_vals.append(vy)
    if not today_vals:
        out.update(max_avwap=np.nan, status="n/a", above=False, crossed_today=False, dist_pct=np.nan)
        return out
    mx = max(today_vals)
    above = close > mx
    was_above = bool(yday_vals) and prev_close > max(yday_vals)
    dist = (close - mx) / mx * 100
    out.update(
        max_avwap=mx,
        max_key=next(k for k in KEYS if k in out["anchors"] and out["anchors"][k]["avwap"] == mx),
        dist_pct=dist,
        above=bool(above),
        crossed_today=bool(above and not was_above),
        status="Above all" if above else ("Near (within %g%%)" % near_pct if dist >= -near_pct else "Below"),
        complete=not out["missing"],
    )
    return out
