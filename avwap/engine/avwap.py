"""Anchored VWAP and volume-weighted sigma bands (sections 3.1 and 3.2).

TP = (H + L + C) / 3
AVWAP(t) = sum(TP*V) / sum(V) from anchor day A to t, including A
sigma(t) = sqrt( sum(V * (TP - AVWAP(t))^2) / sum(V) )
         = sqrt( sum(V*TP^2)/sum(V) - AVWAP(t)^2 )      (algebraically identical)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def typical_price(df: pd.DataFrame) -> pd.Series:
    return (df["high"] + df["low"] + df["close"]) / 3.0


def anchored_vwap(df: pd.DataFrame, anchor) -> pd.DataFrame:
    """Return a frame (indexed like df) with columns avwap, sigma, u1, l1, u2, l2.

    Rows before the anchor are NaN. `anchor` is a date-like or index label.
    """
    anchor = pd.Timestamp(anchor)
    tp = typical_price(df).to_numpy(dtype=float)
    vol = df["volume"].to_numpy(dtype=float)
    mask = (df.index >= anchor)
    n = len(df)
    out = np.full((n, 2), np.nan)
    if mask.any():
        idx0 = int(np.argmax(mask))
        v = vol[idx0:]
        t = tp[idx0:]
        cv = np.cumsum(v)
        cpv = np.cumsum(t * v)
        cpv2 = np.cumsum(t * t * v)
        with np.errstate(invalid="ignore", divide="ignore"):
            vw = cpv / cv
            var = np.maximum(cpv2 / cv - vw * vw, 0.0)
        out[idx0:, 0] = vw
        out[idx0:, 1] = np.sqrt(var)
    res = pd.DataFrame(out, index=df.index, columns=["avwap", "sigma"])
    res["u1"] = res["avwap"] + res["sigma"]
    res["l1"] = res["avwap"] - res["sigma"]
    res["u2"] = res["avwap"] + 2 * res["sigma"]
    res["l2"] = res["avwap"] - 2 * res["sigma"]
    return res


def avwap_matrix(df: pd.DataFrame, anchors) -> pd.DataFrame:
    """AVWAP lines for many anchors at once: columns are anchor keys."""
    cols = {}
    for a in anchors:
        cols[a] = anchored_vwap(df, a)["avwap"]
    return pd.DataFrame(cols, index=df.index)
