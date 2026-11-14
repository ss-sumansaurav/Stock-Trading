"""Automatic anchor detection (section 4.1), manual overrides (4.2), primary selection (4.3)."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

# Display / priority order when several anchor types fall on the same day.
KIND_ORDER = [
    "manual", "entry", "breakout", "major_low", "top", "higher_low", "lower_high",
    "results", "gap", "volume", "p_low", "p_high",
]
LABELS = {
    "p_high": "P-high", "p_low": "P-low", "major_low": "Major low", "top": "Top",
    "higher_low": "Higher low", "lower_high": "Lower high", "volume": "Volume anchor",
    "breakout": "Breakout", "gap": "Gap", "results": "Results", "manual": "Manual",
    "entry": "Entry",
}


@dataclass
class Anchor:
    date: pd.Timestamp
    kind: str                       # main kind (highest priority on that day)
    kinds: list = field(default_factory=list)
    source: str = "auto"            # auto / manual
    price: float | None = None      # pivot price where meaningful
    label_override: str | None = None

    @property
    def label(self) -> str:
        if self.label_override:
            return self.label_override
        return " + ".join(LABELS.get(k, k) for k in self.kinds or [self.kind])

    @property
    def key(self) -> str:
        return f"{self.date:%Y-%m-%d}"

    def to_dict(self):
        d = asdict(self)
        d["date"] = f"{self.date:%Y-%m-%d}"
        d["label"] = self.label
        return d


# ---------------------------------------------------------------- swings
def raw_swings(df: pd.DataFrame, bars: int):
    """Indices that are the highest high / lowest low with `bars` bars on each side."""
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    highs, lows = [], []
    for i in range(bars, n - bars):
        wh = h[i - bars:i + bars + 1]
        wl = l[i - bars:i + bars + 1]
        # first occurrence wins ties so plateaus give one pivot
        if h[i] == wh.max() and np.argmax(wh) == bars:
            highs.append(i)
        if l[i] == wl.min() and np.argmin(wl) == bars:
            lows.append(i)
    return highs, lows


def confirmed_pivots(df: pd.DataFrame, bars: int, min_atr: float, atr: pd.Series):
    """Alternating swing highs/lows where each leg is >= min_atr * ATR(14).

    Returns a list of (index, 'H'|'L', price) in time order.
    """
    highs, lows = raw_swings(df, bars)
    cands = sorted([(i, "H", df["high"].iat[i]) for i in highs] +
                   [(i, "L", df["low"].iat[i]) for i in lows])
    a = atr.to_numpy()
    piv: list[tuple[int, str, float]] = []
    for i, typ, px in cands:
        if not piv:
            piv.append((i, typ, px))
            continue
        j, ltyp, lpx = piv[-1]
        if typ == ltyp:
            # same side again: keep the more extreme one
            if (typ == "H" and px > lpx) or (typ == "L" and px < lpx):
                piv[-1] = (i, typ, px)
            continue
        thr = min_atr * (a[i] if not np.isnan(a[i]) else 0.0)
        if abs(px - lpx) >= thr:
            piv.append((i, typ, px))
    return piv


def higher_lows_lower_highs(df: pd.DataFrame, piv):
    """Higher low: swing low above previous swing low, followed by a high above the
    previous swing high. Lower high: mirror image. Returns lists of indices."""
    highs = [(i, p) for i, t, p in piv if t == "H"]
    lows = [(i, p) for i, t, p in piv if t == "L"]
    h = df["high"].to_numpy()
    lo = df["low"].to_numpy()
    hl, lh = [], []
    for k in range(1, len(lows)):
        i, p = lows[k]
        pi, pp = lows[k - 1]
        if p <= pp:
            continue
        prev_high = [ph for hi, ph in highs if hi < i]
        if not prev_high:
            continue
        if (h[i + 1:] > prev_high[-1]).any():
            hl.append(i)
    for k in range(1, len(highs)):
        i, p = highs[k]
        pi, pp = highs[k - 1]
        if p >= pp:
            continue
        prev_low = [pl for li, pl in lows if li < i]
        if not prev_low:
            continue
        if (lo[i + 1:] < prev_low[-1]).any():
            lh.append(i)
    return hl, lh


# ---------------------------------------------------------------- detection
def detect_anchors(df: pd.DataFrame, cfg: dict, results_dates=()) -> list[Anchor]:
    """df must already carry indicators (atr, avg_vol50). Returns merged anchors."""
    ac = cfg["anchors"]
    n = len(df)
    if n < 2 * ac["swing_bars"] + 2:
        return []
    start = max(0, n - ac["lookback_days"])
    found: dict[int, set] = {}
    price: dict[int, float] = {}

    def add(i, kind, px=None):
        if i < start:
            return
        found.setdefault(i, set()).add(kind)
        if px is not None:
            price.setdefault(i, px)

    atr = df["atr"]
    # Pivots are computed on a slightly longer window so the first leg has a reference.
    pad = max(0, start - 60)
    sub = df.iloc[pad:]
    piv = [(i + pad, t, p) for i, t, p in
           confirmed_pivots(sub, ac["swing_bars"], ac["swing_min_atr"], atr.iloc[pad:])]
    for i, t, p in piv:
        add(i, "p_high" if t == "H" else "p_low", p)
    hl, lh = higher_lows_lower_highs(df, piv)
    for i in hl:
        add(i, "higher_low", df["low"].iat[i])
    for i in lh:
        add(i, "lower_high", df["high"].iat[i])

    win = df.iloc[start:]
    add(start + int(np.argmin(win["low"].to_numpy())), "major_low", win["low"].min())
    add(start + int(np.argmax(win["high"].to_numpy())), "top", win["high"].max())

    vol = df["volume"].to_numpy()
    av50 = df["avg_vol50"].to_numpy()
    prior_hi = df["high"].shift(1).rolling(ac["breakout_lookback"]).max().to_numpy()
    close = df["close"].to_numpy()
    op = df["open"].to_numpy()
    ph = df["high"].shift(1).to_numpy()
    pl = df["low"].shift(1).to_numpy()
    patr = atr.shift(1).to_numpy()
    for i in range(start, n):
        if av50[i] > 0 and vol[i] >= ac["volume_anchor_mult"] * av50[i]:
            add(i, "volume")
            if not np.isnan(prior_hi[i]) and close[i] > prior_hi[i]:
                add(i, "breakout")
        if not np.isnan(patr[i]) and patr[i] > 0:
            g = ac["gap_min_atr"] * patr[i]
            if op[i] >= ph[i] + g or op[i] <= pl[i] - g:
                add(i, "gap")

    idx = df.index
    for d in results_dates:
        d = pd.Timestamp(d)
        pos = idx.searchsorted(d)
        if pos < n:
            add(int(pos), "results")

    anchors = []
    for i, kinds in found.items():
        ks = sorted(kinds, key=KIND_ORDER.index)
        anchors.append(Anchor(date=idx[i], kind=ks[0], kinds=ks, price=price.get(i)))
    anchors.sort(key=lambda a: a.date)
    return _trim(anchors, ac)


def _trim(anchors: list[Anchor], ac: dict) -> list[Anchor]:
    """Keep the structural anchors and the most recent others, so charts stay readable."""
    must = set()
    for kind in ("major_low", "top"):
        for a in anchors:
            if kind in a.kinds:
                must.add(a.date)
    for kind in ("breakout", "higher_low", "lower_high"):
        latest = [a for a in anchors if kind in a.kinds]
        for a in latest[-2:]:
            must.add(a.date)
    others = [a for a in anchors if a.date not in must]
    room = max(0, ac.get("max_auto_anchors", 12) - len(must))
    keep = must | {a.date for a in others[-room:]} if room else must
    return [a for a in anchors if a.date in keep]


# ---------------------------------------------------------------- overrides
def apply_manual(anchors: list[Anchor], manual: pd.DataFrame, index: pd.DatetimeIndex) -> list[Anchor]:
    """manual rows: date, label, action (add/delete). Deleting hides the auto anchor on that date."""
    by_date = {a.date: a for a in anchors}
    if manual is not None and len(manual):
        for r in manual.sort_values("id").itertuples():
            d = pd.Timestamp(r.date)
            pos = index.searchsorted(d)
            if pos >= len(index):
                continue
            d = index[pos]  # snap to a trading day
            if r.action == "delete":
                by_date.pop(d, None)
            else:
                by_date[d] = Anchor(date=d, kind="manual", kinds=["manual"], source="manual",
                                    label_override=r.label or "Manual")
    return sorted(by_date.values(), key=lambda a: a.date)


# ---------------------------------------------------------------- primary
def choose_primary(df: pd.DataFrame, anchors: list[Anchor], cfg: dict, pin_date=None):
    """Section 4.3. Returns (anchor, reason)."""
    if not anchors:
        return None, "no anchors"
    idx = df.index
    if pin_date is not None:
        pd_ = pd.Timestamp(pin_date)
        for a in anchors:
            if a.date == pd_:
                return a, "pinned"
    recent_cut = idx[max(0, len(idx) - cfg["anchors"]["breakout_recent_days"])]
    bos = [a for a in anchors if "breakout" in a.kinds and a.date >= recent_cut]
    if bos:
        return bos[-1], "recent breakout"
    hls = [a for a in anchors if "higher_low" in a.kinds]
    lhs = [a for a in anchors if "lower_high" in a.kinds]
    last_hl = hls[-1] if hls else None
    last_lh = lhs[-1] if lhs else None
    # Uptrend: most recent higher low, newer than any lower high, never undercut since.
    if last_hl and (last_lh is None or last_hl.date > last_lh.date):
        ref = last_hl.price if last_hl.price is not None else df.loc[last_hl.date, "low"]
        since = df["low"].loc[last_hl.date:].iloc[1:]
        if since.empty or since.min() >= ref:
            return last_hl, "higher low (uptrend)"
    # Downtrend: most recent lower high, newer than any higher low, never exceeded since.
    if last_lh and (last_hl is None or last_lh.date > last_hl.date):
        ref = last_lh.price if last_lh.price is not None else df.loc[last_lh.date, "high"]
        since = df["high"].loc[last_lh.date:].iloc[1:]
        if since.empty or since.max() <= ref:
            return last_lh, "lower high (downtrend)"
    for a in anchors:
        if "major_low" in a.kinds:
            return a, "12-month major low"
    return anchors[0], "earliest anchor"


def major_low_anchor(anchors: list[Anchor]):
    for a in anchors:
        if "major_low" in a.kinds:
            return a
    return None
