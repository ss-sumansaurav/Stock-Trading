import numpy as np
import pandas as pd

from avwap.config import with_overrides
from avwap.demo import synth_ohlcv
from avwap.engine.analysis import analyze
from avwap.engine.anchors import choose_primary, detect_anchors, apply_manual
from avwap.engine.clusters import find_clusters
from avwap.engine.indicators import add_indicators


def test_major_low_top_and_volume_anchor(cfg):
    df = synth_ohlcv(11, 400)
    spike = df.index[-30]
    df.loc[spike, "volume"] = df["volume"].iloc[-80:-31].mean() * 8
    d = add_indicators(df, cfg)
    anchors = detect_anchors(d, cfg)
    win = d.iloc[-cfg["anchors"]["lookback_days"]:]
    kinds = {a.date: a.kinds for a in anchors}
    assert "major_low" in kinds[win["low"].idxmin()]
    assert "top" in kinds[win["high"].idxmax()]
    assert "volume" in kinds[spike]
    # nothing older than the 12-month window
    assert min(kinds) >= win.index[0]


def test_breakout_is_primary_and_pin_overrides(cfg):
    df = synth_ohlcv(5, 400)
    i = len(df) - 15
    bo = df.index[i]
    df.loc[bo, "close"] = df["high"].iloc[i - 20:i].max() * 1.03
    df.loc[bo, "high"] = df.loc[bo, "close"] * 1.005
    df.loc[bo, "volume"] = df["volume"].iloc[i - 50:i].mean() * 6
    d = add_indicators(df, cfg)
    anchors = detect_anchors(d, cfg)
    p, why = choose_primary(d, anchors, cfg)
    assert p.date == bo and why == "recent breakout"
    other = anchors[0]
    p2, why2 = choose_primary(d, anchors, cfg, pin_date=other.date)
    assert p2.date == other.date and why2 == "pinned"


def test_manual_add_and_delete(cfg):
    df = add_indicators(synth_ohlcv(2, 300), cfg)
    auto = detect_anchors(df, cfg)
    victim = auto[0].key
    manual = pd.DataFrame({"id": [1, 2], "date": [f"{df.index[-10]:%Y-%m-%d}", victim],
                           "label": ["My level", None], "action": ["add", "delete"]})
    out = apply_manual(auto, manual, df.index)
    keys = {a.key: a for a in out}
    assert victim not in keys
    assert keys[f"{df.index[-10]:%Y-%m-%d}"].source == "manual"


def test_clusters():
    cl = find_clusters({"a": 100, "b": 101.2, "c": 103, "d": 110, "e": 110.5}, 1.5, close=105)
    assert [c["members"] for c in cl] == [["a", "b"], ["d", "e"]]
    assert cl[0]["position"] == "support" and cl[1]["position"] == "overhead"


def _flat_then(cfg, last_close, last_low=None, n=120):
    idx = pd.bdate_range("2025-01-01", periods=n)
    base = np.full(n, 100.0)
    df = pd.DataFrame({"open": base, "high": base + 1, "low": base - 1, "close": base,
                       "volume": np.full(n, 1e5)}, index=idx)
    df.iloc[-2, df.columns.get_loc("close")] = 99.0
    df.iloc[-1, df.columns.get_loc("close")] = last_close
    df.iloc[-1, df.columns.get_loc("high")] = max(last_close + 0.5, 101)
    df.iloc[-1, df.columns.get_loc("low")] = last_low if last_low is not None else 99
    return df


def test_primary_reclaim_and_pullback_signals(cfg):
    df = _flat_then(cfg, 101.5, last_low=100.0)
    manual = pd.DataFrame({"id": [1], "date": [f"{df.index[10]:%Y-%m-%d}"], "label": ["M"], "action": ["add"]})
    an = analyze("X", df, cfg, manual=manual, pin_date=f"{df.index[10]:%Y-%m-%d}")
    sig = {s["signal"] for s in an.signals}
    assert an.primary_reason == "pinned"
    assert "primary_reclaim" in sig
    assert "pullback_hold" in sig


def test_primary_loss_and_volume_confirmation(cfg):
    df = _flat_then(cfg, 97.0)
    df.iloc[-2, df.columns.get_loc("close")] = 101.0
    df.iloc[-1, df.columns.get_loc("volume")] = 3e5
    d10 = f"{df.index[10]:%Y-%m-%d}"
    manual = pd.DataFrame({"id": [1], "date": [d10], "label": ["M"], "action": ["add"]})
    an = analyze("X", df, cfg, manual=manual, pin_date=d10)
    loss = [s for s in an.signals if s["signal"] == "primary_loss"]
    assert loss and loss[0]["vol_confirmed"]
