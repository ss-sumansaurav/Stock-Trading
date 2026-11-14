import numpy as np
import pandas as pd

from avwap.data.corporate_actions import adjust_prices, parse_subject
from avwap.demo import synth_ohlcv
from avwap.engine.avwap import anchored_vwap


def test_parse_subjects():
    assert parse_subject("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share") == [("split", 5.0)]
    assert parse_subject("Bonus 1:1") == [("bonus", 2.0)]
    assert parse_subject("Bonus 3:2") == [("bonus", 2.5)]
    assert parse_subject("Dividend - Rs 5 Per Share") == []
    k = parse_subject("Consolidation of shares from Rs 1 to Rs 10")
    assert k and k[0][0] == "consolidation" and abs(k[0][1] - 0.1) < 1e-12


def test_split_adjustment_removes_jump():
    true = synth_ohlcv(3, 300)
    ex = true.index[200]
    raw = true.copy()
    pre = raw.index < ex
    raw.loc[pre, ["open", "high", "low", "close"]] *= 5
    raw.loc[pre, "volume"] /= 5
    acts = pd.DataFrame({"ex_date": [ex], "factor": [5.0]})
    adj = adjust_prices(raw, acts)
    np.testing.assert_allclose(adj.to_numpy(), true.to_numpy(), rtol=1e-12)
    # AVWAP across the split date has no jump (day-over-day change stays small)
    a = anchored_vwap(adj, adj.index[100])["avwap"]
    jump = abs(a.loc[ex] / a.shift(1).loc[ex] - 1)
    assert jump < 0.02
    # while unadjusted data would jump massively
    b = anchored_vwap(raw, raw.index[100])["avwap"]
    assert abs(b.loc[ex] / b.shift(1).loc[ex] - 1) > 0.05
