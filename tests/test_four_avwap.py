import numpy as np
import pandas as pd

from avwap.demo import synth_ohlcv
from avwap.engine.avwap import anchored_vwap
from avwap.engine.four_avwap import four_avwap, results_anchor, volume_anchor
from avwap.engine.indicators import add_indicators


def test_volume_anchor_skips_diluted_days():
    idx = pd.bdate_range("2026-01-01", periods=10)
    df = pd.DataFrame({"volume": [1000, 10, 10, 10, 10, 10, 10, 10, 50, 10]}, index=idx)
    # day 0 = 1000 of 1140 total since -> qualifies at 10%
    i, share = volume_anchor(df, 0, 10)
    assert i == 0 and share > 80
    df.iloc[1:, 0] = 2000  # now day 0 is small; biggest days are diluted by later ones
    i, share = volume_anchor(df, 0, 10)
    assert i is not None and share >= 10
    tail = df["volume"].iloc[i:].sum()
    assert df["volume"].iat[i] / tail * 100 >= 10


def test_results_anchor_next_trading_day():
    idx = pd.bdate_range("2026-01-01", periods=30)
    pos, d = results_anchor(idx, ["2026-01-09", "2025-10-10"], next_day=True)  # Friday
    assert idx[pos] == pd.Timestamp("2026-01-12")  # Monday
    pos, _ = results_anchor(idx, ["2026-01-09"], next_day=False)
    assert idx[pos] == pd.Timestamp("2026-01-09")
    assert results_anchor(idx, [], True) == (None, None)


def test_four_avwap_values_and_status(cfg):
    df = add_indicators(synth_ohlcv(21, 400), cfg)
    res_day = df.index[-40]
    f = four_avwap(df, cfg, [res_day])
    assert set(f["anchors"]) == {"high", "low", "results", "volume"} and f["complete"]
    win = df.iloc[-252:]
    assert f["anchors"]["high"]["date"] == f"{win['high'].idxmax():%Y-%m-%d}"
    assert f["anchors"]["low"]["date"] == f"{win['low'].idxmin():%Y-%m-%d}"
    assert f["anchors"]["results"]["date"] == f"{df.index[-39]:%Y-%m-%d}"
    exp = anchored_vwap(df, win["high"].idxmax())["avwap"].iat[-1]
    assert abs(f["anchors"]["high"]["avwap"] - exp) < 1e-9
    mx = max(a["avwap"] for a in f["anchors"].values())
    assert f["max_avwap"] == mx and f["above"] == (df["close"].iat[-1] > mx)


def test_cross_detected(cfg):
    idx = pd.bdate_range("2025-01-01", periods=300)
    px = np.r_[np.linspace(100, 80, 200), np.full(99, 80.0), [90.0]]
    df = pd.DataFrame({"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                       "volume": np.full(300, 1e5)}, index=idx)
    df.iloc[-1, df.columns.get_loc("volume")] = 3e5  # today is the volume-day anchor
    df.iloc[-1, df.columns.get_loc("high")] = 90.0     # closed at the high, so above its own TP
    df.iloc[-1, df.columns.get_loc("low")] = 88.0
    f = four_avwap(add_indicators(df, cfg), cfg, [])
    assert f["above"] and f["crossed_today"] and f["missing"] == ["results"]
