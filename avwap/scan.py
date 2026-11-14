"""Full-universe scan: adjust prices, analyse every stock, store results, evaluate positions."""
from __future__ import annotations

import json
import logging
import time
from datetime import date

import numpy as np
import pandas as pd

from .data.pipeline import rebuild_adjusted
from .db import DB
from .engine.analysis import analyze
from .engine.positions import evaluate_position

log = logging.getLogger(__name__)


def _jsonable(o):
    if isinstance(o, (np.floating, float)):
        return None if o != o else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.Timestamp):
        return f"{o:%Y-%m-%d}"
    raise TypeError(type(o))


def stock_inputs(db: DB, symbol: str):
    manual = db.q("SELECT id, date, label, action FROM manual_anchors WHERE symbol=?", (symbol,))
    pin = db.q("SELECT anchor_date FROM pins WHERE symbol=?", (symbol,))
    res = db.q("SELECT date FROM results_dates WHERE symbol=?", (symbol,))["date"].tolist()
    pos = db.q("SELECT entry_date FROM positions WHERE symbol=? AND closed=0", (symbol,))
    extra = {d: "Entry" for d in pos["entry_date"]}
    return manual, (pin["anchor_date"].iat[0] if len(pin) else None), res, extra


def analyze_symbol(db: DB, cfg: dict, symbol: str, prices: pd.DataFrame | None = None):
    if prices is None:
        prices = db.adj_prices(symbol)
    manual, pin, res, extra = stock_inputs(db, symbol)
    return analyze(symbol, prices, cfg, manual, pin, res, extra)


def run_scan(db: DB, cfg: dict, trade_date: date | None = None, rebuild: bool = True) -> dict:
    t0 = time.time()
    uni = db.active_universe()
    if uni.empty:
        raise RuntimeError("Universe is empty: run `python -m avwap universe <file>` first")
    results, errors = [], []
    scan_date = None
    for r in uni.itertuples(index=False):
        sym = r.nse_symbol
        try:
            if rebuild:
                prices = rebuild_adjusted(db, r.isin, sym, "BSE" if r.exchange == "BSE" else "NSE")
            else:
                prices = db.adj_prices(sym)
            if trade_date is not None:
                prices = prices[prices.index <= pd.Timestamp(trade_date)]
            if trade_date is not None and (prices.empty or prices.index[-1] != pd.Timestamp(trade_date)):
                continue  # no bar today (suspended / not traded)
            an = analyze_symbol(db, cfg, sym, prices)
            if an is None:
                continue
            s = an.summary()
            s.update({"sector": r.sector, "exchange": r.exchange, "company": r.company,
                      "anchors": [a.to_dict() for a in an.anchors]})
            results.append(s)
            scan_date = max(scan_date or s["date"], s["date"])
        except Exception as e:  # keep scanning other stocks
            log.exception("Scan failed for %s", sym)
            errors.append((sym, str(e)))
    if not results:
        raise RuntimeError("Scan produced no results")
    scan_date = str(trade_date) if trade_date else scan_date
    store_results(db, scan_date, results)
    pos = evaluate_positions(db, cfg, trade_date)
    return {"date": scan_date, "results": results, "positions": pos, "errors": errors,
            "seconds": round(time.time() - t0, 1)}


def store_results(db: DB, d: str, results: list[dict]):
    with db.tx() as c:
        c.execute("DELETE FROM scan_results WHERE date=?", (d,))
        c.execute("DELETE FROM signals WHERE date=?", (d,))
        for s in results:
            if s["date"] != d:
                continue
            cl = [x for x in s["clusters"]]
            cdesc = "; ".join(f"{x['position']} {x['low']:.2f}-{x['high']:.2f} ({len(x['members'])})" for x in cl)
            c.execute(
                "INSERT INTO scan_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d, s["symbol"], s["close"], s["primary_label"], s["primary_date"],
                 _jsonable(s["primary_avwap"]), _jsonable(s["primary_dist_pct"]),
                 int(bool(s["above_primary"])), _jsonable(s["major_low_avwap"]),
                 int(s["near_cluster"]), cdesc, _jsonable(s["vol_ratio"]),
                 ", ".join(x["name"] + (" (vol)" if x["vol_confirmed"] else "") for x in s["signals"]),
                 s.get("sector"), s.get("exchange"), json.dumps(s, default=_jsonable)))
            for x in s["signals"]:
                c.execute("INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?)",
                          (d, s["symbol"], x["signal"], x["detail"], int(x["vol_confirmed"])))


def evaluate_positions(db: DB, cfg: dict, trade_date: date | None = None) -> list[dict]:
    out = []
    pos = db.q("SELECT * FROM positions WHERE closed=0")
    for p in pos.to_dict("records"):
        prices = db.adj_prices(p["symbol"])
        if trade_date is not None:
            prices = prices[prices.index <= pd.Timestamp(trade_date)]
        an = analyze_symbol(db, cfg, p["symbol"], prices)
        if an is None or len(an.df) < 2:
            continue
        ev = evaluate_position(p, an)
        if ev["new_hl"]:
            db.exec("UPDATE positions SET last_hl_seen=? WHERE id=?", (ev["new_hl"], p["id"]))
        out.append(ev)
    return out
