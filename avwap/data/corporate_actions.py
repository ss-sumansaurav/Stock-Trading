"""Corporate-action parsing and back-adjustment (section 2.3).

For every split / bonus / consolidation with ex-date E and factor f (new shares per old):
prices before E are divided by f and volumes multiplied by f. Raw data is never changed;
adjusted data lives in prices_adj.
"""
from __future__ import annotations

import re

import pandas as pd

_SPLIT = re.compile(r"(?:split|sub-?division).*?(?:rs\.?|re\.?|inr)?\s*([\d.]+)\s*/?-?.*?to\s*(?:rs\.?|re\.?|inr)?\s*([\d.]+)", re.I)
_BONUS = re.compile(r"bonus[^\d]*(\d+)\s*:\s*(\d+)", re.I)
_CONSOL = re.compile(r"consolidat.*?(?:rs\.?|re\.?)?\s*([\d.]+).*?to\s*(?:rs\.?|re\.?)?\s*([\d.]+)", re.I)


def parse_subject(subject: str):
    """Return list of (kind, factor) found in an NSE corporate action subject line."""
    out = []
    s = subject or ""
    m = _BONUS.search(s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if b > 0:
            out.append(("bonus", (a + b) / b))
    m = _SPLIT.search(s)
    if m and "consolidat" not in s.lower():
        old, new = float(m.group(1)), float(m.group(2))
        if new > 0 and old > new:
            out.append(("split", old / new))
    m = _CONSOL.search(s)
    if m:
        old, new = float(m.group(1)), float(m.group(2))
        if old > 0 and new > old:
            out.append(("consolidation", old / new))
    return out


def adjust_prices(raw: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """raw: OHLCV indexed by date. actions: ex_date, factor."""
    adj = raw.copy().astype(float)
    if actions is None or not len(actions):
        return adj
    for r in actions.itertuples():
        ex = pd.Timestamp(r.ex_date)
        f = float(r.factor)
        if f <= 0 or f == 1:
            continue
        m = adj.index < ex
        adj.loc[m, ["open", "high", "low", "close"]] /= f
        adj.loc[m, "volume"] *= f
    return adj
