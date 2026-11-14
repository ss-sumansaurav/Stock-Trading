"""Run the full per-stock analysis: indicators -> anchors -> AVWAPs -> primary -> clusters -> signals."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .anchors import Anchor, apply_manual, choose_primary, detect_anchors, major_low_anchor
from .avwap import anchored_vwap, avwap_matrix
from .clusters import find_clusters
from .indicators import add_indicators
from .signals import compute_signals


@dataclass
class StockAnalysis:
    symbol: str
    df: pd.DataFrame
    anchors: list
    lines: pd.DataFrame
    primary: Anchor | None
    primary_reason: str
    primary_band: pd.DataFrame | None
    major_low: Anchor | None
    clusters: list
    signals: list
    extra: dict = field(default_factory=dict)

    def summary(self) -> dict:
        last = self.df.iloc[-1]
        close = float(last["close"])
        p = float(self.primary_band["avwap"].iat[-1]) if self.primary_band is not None else np.nan
        ml = (float(self.lines[self.major_low.key].iat[-1])
              if self.major_low is not None and self.major_low.key in self.lines else np.nan)
        av20 = last.get("avg_vol20", np.nan)
        in_cl = [c for c in self.clusters if c["low"] <= close * 1.03 and c["high"] >= close * 0.97]
        return {
            "symbol": self.symbol,
            "date": f"{self.df.index[-1]:%Y-%m-%d}",
            "close": close,
            "primary_label": self.primary.label if self.primary else None,
            "primary_date": self.primary.key if self.primary else None,
            "primary_reason": self.primary_reason,
            "primary_avwap": p,
            "primary_dist_pct": (close - p) / p * 100 if p == p and p else np.nan,
            "above_primary": bool(close > p) if p == p else None,
            "major_low_avwap": ml,
            "vol_ratio": float(last["volume"] / av20) if av20 and av20 == av20 else np.nan,
            "clusters": self.clusters,
            "near_cluster": bool(in_cl),
            "signals": self.signals,
            "rsi": float(last.get("rsi", np.nan)),
            "atr": float(last.get("atr", np.nan)),
        }


def analyze(symbol: str, prices: pd.DataFrame, cfg: dict, manual=None, pin_date=None,
            results_dates=(), extra_anchors: dict | None = None) -> StockAnalysis | None:
    """prices: adjusted OHLCV indexed by date. extra_anchors: {date: label} (e.g. entries)."""
    if prices is None or len(prices) < 30:
        return None
    df = add_indicators(prices, cfg)
    anchors = detect_anchors(df, cfg, results_dates)
    anchors = apply_manual(anchors, manual, df.index)
    for d, label in (extra_anchors or {}).items():
        d = pd.Timestamp(d)
        pos = df.index.searchsorted(d)
        if pos < len(df) and not any(a.date == df.index[pos] for a in anchors):
            anchors.append(Anchor(date=df.index[pos], kind="entry", kinds=["entry"],
                                  source="manual", label_override=label))
    anchors.sort(key=lambda a: a.date)
    lines = avwap_matrix(df, [a.date for a in anchors])
    lines.columns = [a.key for a in anchors]
    primary, reason = choose_primary(df, [a for a in anchors if a.kind != "entry"], cfg, pin_date)
    band = anchored_vwap(df, primary.date) if primary else None
    clusters = find_clusters(lines.iloc[-1].dropna().to_dict(), cfg["clusters"]["max_spread_pct"],
                             float(df["close"].iat[-1]))
    for c in clusters:
        c["labels"] = [next(a.label for a in anchors if a.key == k) for k in c["members"]]
    sigs = compute_signals(df, lines, primary.key if primary else None, band, anchors, cfg) \
        if primary is not None else []
    return StockAnalysis(symbol, df, anchors, lines, primary, reason, band,
                         major_low_anchor(anchors), clusters, sigs)
