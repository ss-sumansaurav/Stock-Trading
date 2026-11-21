"""Synthetic demo data so the dashboard and tests can run without NSE access.

Creates fake stocks (DEMO01...) with trends, pullbacks, volume spikes, gaps and one 1:5 split.
Never mix with real data: use a separate database (settings paths.db).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .db import DB

SECTORS = ["IT", "Banks", "Pharma", "Auto", "FMCG", "Metals", "Energy", "Capital Goods"]


def synth_ohlcv(seed: int, days: int = 600, start_price: float = 500.0,
                end: str | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=end or pd.Timestamp.today().normalize(), periods=days)
    # regime-switching drift gives real swings
    drift = np.repeat(rng.normal(0, 0.003, days // 40 + 1), 40)[:days]
    ret = drift + rng.normal(0, 0.017, days)
    close = start_price * np.exp(np.cumsum(ret))
    gap = np.zeros(days)
    for g in rng.choice(np.arange(50, days), 4, replace=False):
        gap[g] = rng.choice([-1, 1]) * rng.uniform(0.04, 0.08)
    close = close * np.exp(np.cumsum(gap))
    prev = np.r_[close[0], close[:-1]]
    op = prev * np.exp(gap + rng.normal(0, 0.004, days))
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.009, days)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.009, days)))
    vol = rng.lognormal(13, 0.35, days)
    spikes = rng.choice(np.arange(60, days), 6, replace=False)
    vol[spikes] *= rng.uniform(3.5, 6, len(spikes))
    vol[gap != 0] *= 3.5
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close, "volume": vol.round()},
                        index=idx)


def load_demo(db: DB, n: int = 40, days: int = 600, seed0: int = 7):
    rng = np.random.default_rng(seed0)
    with db.tx() as c:
        for k in range(n):
            sym = f"DEMO{k + 1:02d}"
            isin = f"INEDEMO{k + 1:05d}"
            exch = "BSE" if k == n - 1 else "NSE"
            symbol = f"BSE-5000{k:02d}" if exch == "BSE" else sym
            c.execute("INSERT OR REPLACE INTO universe (isin, company, bse_code, nse_symbol, sector, exchange,"
                      " active, added_on) VALUES (?,?,?,?,?,?,1,date('now'))",
                      (isin, f"Demo Company {k + 1}", f"5000{k:02d}", symbol, SECTORS[k % len(SECTORS)], exch))
            df = synth_ohlcv(seed0 + k, days, float(rng.uniform(80, 3000)))
            raw = df.copy()
            if k == 0:  # simulate a 1:5 split 120 days ago in the raw data
                ex = df.index[-120]
                pre = raw.index < ex
                raw.loc[pre, ["open", "high", "low", "close"]] *= 5
                raw.loc[pre, "volume"] /= 5
                c.execute("INSERT OR REPLACE INTO corp_actions VALUES (?,?,?,?,?,?)",
                          (isin, symbol, f"{ex:%Y-%m-%d}", "split", 5.0,
                           "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"))
            c.executemany("INSERT OR REPLACE INTO prices_raw VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [(isin, f"{d:%Y-%m-%d}", symbol, r.open, r.high, r.low, r.close, r.volume,
                            "EQ", exch) for d, r in zip(raw.index, raw.itertuples())])
            # quarterly results roughly every 63 sessions (none for the last stock: shows 3/4)
            if k != n - 2:
                c.executemany("INSERT OR REPLACE INTO results_dates VALUES (?,?,?)",
                              [(symbol, f"{d:%Y-%m-%d}", "DEMO") for d in df.index[-(20 + k % 30)::-63][:6]])
        days_idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
        c.executemany("INSERT OR REPLACE INTO loaded_days VALUES (?,?,?,datetime('now'))",
                      [(f"{d:%Y-%m-%d}", "DEMO", n) for d in days_idx])
    # a couple of demo positions
    db.exec("DELETE FROM positions")
    last = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    db.exec("INSERT INTO positions (symbol, entry_date, entry_price, qty) VALUES (?,?,?,?)",
            ("DEMO02", f"{last[-70]:%Y-%m-%d}", 0, 100))
    db.exec("INSERT INTO positions (symbol, entry_date, entry_price, qty) VALUES (?,?,?,?)",
            ("DEMO05", f"{last[-35]:%Y-%m-%d}", 0, 50))
    # set entry prices to that day's close
    for r in db.q("SELECT id, symbol, entry_date FROM positions").itertuples():
        px = db.q("SELECT close FROM prices_raw WHERE symbol=? AND date=?", (r.symbol, r.entry_date))
        if len(px):
            db.exec("UPDATE positions SET entry_price=? WHERE id=?", (float(px["close"].iat[0]), r.id))
