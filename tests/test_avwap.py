import numpy as np
import pandas as pd

from avwap.demo import synth_ohlcv
from avwap.engine.avwap import anchored_vwap


def naive(df, anchor):
    sub = df[df.index >= anchor]
    tp = (sub.high + sub.low + sub.close) / 3
    vw, sd = [], []
    for i in range(len(sub)):
        t, v = tp.iloc[: i + 1], sub.volume.iloc[: i + 1]
        m = (t * v).sum() / v.sum()
        vw.append(m)
        sd.append(np.sqrt((v * (t - m) ** 2).sum() / v.sum()))
    return np.array(vw), np.array(sd)


def test_avwap_and_sigma_match_definition():
    df = synth_ohlcv(1, 200)
    anchor = df.index[50]
    got = anchored_vwap(df, anchor)
    vw, sd = naive(df, anchor)
    assert got["avwap"].iloc[:50].isna().all()
    np.testing.assert_allclose(got["avwap"].iloc[50:].to_numpy(), vw, rtol=1e-10)
    np.testing.assert_allclose(got["sigma"].iloc[50:].to_numpy(), sd, rtol=1e-6, atol=1e-6)
    np.testing.assert_allclose(got["u2"] - got["avwap"], 2 * got["sigma"])


def test_anchor_day_included_and_hand_example():
    idx = pd.bdate_range("2026-01-01", periods=3)
    df = pd.DataFrame({"open": [10, 11, 12], "high": [12, 13, 14], "low": [9, 10, 11],
                       "close": [11, 12, 13], "volume": [100, 300, 0]}, index=idx)
    r = anchored_vwap(df, idx[0])
    tp = [32 / 3, 35 / 3, 38 / 3]
    assert abs(r["avwap"].iat[0] - tp[0]) < 1e-12  # first day = its own typical price
    assert abs(r["avwap"].iat[1] - (tp[0] * 100 + tp[1] * 300) / 400) < 1e-12
    assert abs(r["avwap"].iat[2] - r["avwap"].iat[1]) < 1e-12  # zero volume day changes nothing
