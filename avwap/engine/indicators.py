"""Supporting indicators (section 3.3). Wilder smoothing to match TradingView/StockEdge."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return rma(tr, n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = rma(d.clip(lower=0), n)
    down = rma((-d).clip(lower=0), n)
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down != 0, 100.0)


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    ic = cfg["indicators"]
    out = df.copy()
    out["atr"] = atr(out, ic["atr_period"])
    out["rsi"] = rsi(out["close"], ic["rsi_period"])
    out["ema20"] = out["close"].ewm(span=ic["ema_period"], adjust=False).mean()
    out["sma10"] = out["close"].rolling(ic["sma_period"]).mean()
    # Averages of the *prior* N days, so today's spike is compared with normal volume.
    out["avg_vol20"] = out["volume"].shift(1).rolling(ic["avg_vol_short"], min_periods=5).mean()
    out["avg_vol50"] = out["volume"].shift(1).rolling(ic["avg_vol_long"], min_periods=10).mean()
    out["hi52"] = out["high"].rolling(252, min_periods=1).max()
    out["lo52"] = out["low"].rolling(252, min_periods=1).min()
    return out
