"""Data pipeline: universe, holidays, daily bhavcopy, backfill, corporate actions, results dates."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from ..db import DB
from .corporate_actions import adjust_prices, parse_subject
from .nse import (NSE_BOARD_MEETINGS, NSE_CORP_ACTIONS, NSE_HOLIDAYS, NotPublished, NSESession,
                  fetch_bse_bhavcopy, fetch_nse_bhavcopy, fetch_nse_equity_list)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ universe
def _pick(cols, *names):
    low = {c.lower().strip(): c for c in cols}
    for n in names:
        for lc, c in low.items():
            if n in lc:
                return c
    return None


def import_universe(db: DB, csv_path: str, sess: NSESession | None = None) -> dict:
    """Load a BSE 500 constituents file (CSV or Excel from asiaindex.com / bseindia.com).

    Needs an ISIN column; company, scrip code, sector/industry and symbol are optional.
    Maps ISIN -> NSE symbol via NSE's EQUITY_L.csv. Keeps history of additions/removals.
    """
    if csv_path.lower().endswith((".xls", ".xlsx")):
        src = pd.read_excel(csv_path)
    else:
        src = pd.read_csv(csv_path)
    c_isin = _pick(src.columns, "isin")
    if not c_isin:
        raise ValueError("Universe file needs an ISIN column")
    c_name = _pick(src.columns, "company", "name", "security")
    c_code = _pick(src.columns, "scrip code", "security code", "code")
    c_sec = _pick(src.columns, "sector", "industry")
    c_sym = _pick(src.columns, "nse symbol", "symbol", "ticker")
    new = pd.DataFrame({
        "isin": src[c_isin].astype(str).str.strip(),
        "company": src[c_name].astype(str).str.strip() if c_name else "",
        "bse_code": src[c_code].astype(str).str.strip() if c_code else "",
        "sector": src[c_sec].astype(str).str.strip() if c_sec else "",
        "nse_symbol": src[c_sym].astype(str).str.strip() if c_sym else None,
    }).drop_duplicates("isin")

    nse = None
    if sess is not None:
        try:
            nse = fetch_nse_equity_list(sess)
        except Exception as e:  # network optional
            log.warning("Could not fetch NSE equity list (%s); using symbols from the file", e)
    if nse is not None:
        m = dict(zip(nse["isin"], nse["nse_symbol"]))
        new["nse_symbol"] = new["isin"].map(m).fillna(new["nse_symbol"])
        new["exchange"] = new["isin"].map(lambda i: "NSE" if i in m else "BSE")
    else:
        new["exchange"] = new["nse_symbol"].map(lambda s: "NSE" if s and s != "nan" else "BSE")
    # BSE-only stocks are shown with a BSE- prefix on their scrip code.
    bse_only = new["exchange"] == "BSE"
    new.loc[bse_only, "nse_symbol"] = "BSE-" + new.loc[bse_only, "bse_code"].astype(str)

    today = date.today().isoformat()
    old = db.q("SELECT isin FROM universe WHERE active=1")
    old_set, new_set = set(old["isin"]), set(new["isin"])
    added, removed = new_set - old_set, old_set - new_set
    with db.tx() as c:
        for r in new.itertuples(index=False):
            c.execute(
                "INSERT INTO universe (isin, company, bse_code, nse_symbol, sector, exchange, active, added_on)"
                " VALUES (?,?,?,?,?,?,1,?) ON CONFLICT(isin) DO UPDATE SET company=excluded.company,"
                " bse_code=excluded.bse_code, nse_symbol=excluded.nse_symbol, sector=excluded.sector,"
                " exchange=excluded.exchange, active=1, removed_on=NULL",
                (r.isin, r.company, r.bse_code, r.nse_symbol, r.sector, r.exchange, today))
        for i in removed:
            c.execute("UPDATE universe SET active=0, removed_on=? WHERE isin=?", (today, i))
        names = dict(zip(new["isin"], new["company"]))
        for i in added:
            c.execute("INSERT INTO universe_history VALUES (?,?,?,?)", (today, i, names.get(i), "added"))
        for i in removed:
            c.execute("INSERT INTO universe_history VALUES (?,?,?,?)", (today, i, None, "removed"))
    not_on_nse = new.loc[bse_only, ["isin", "company"]].values.tolist()
    for isin, comp in not_on_nse:
        log.warning("Not listed on NSE, will use BSE bhavcopy: %s %s", isin, comp)
    return {"total": len(new), "added": len(added), "removed": len(removed), "bse_only": len(not_on_nse)}


# ------------------------------------------------------------------ holidays
def refresh_holidays(db: DB, sess: NSESession, extra=()):
    rows = [(d, "settings") for d in extra]
    try:
        js = sess.get_json(NSE_HOLIDAYS)
        for h in js.get("CM", []):
            d = datetime.strptime(h["tradingDate"], "%d-%b-%Y").date().isoformat()
            rows.append((d, h.get("description", "")))
    except Exception as e:
        log.warning("Holiday list download failed: %s", e)
    if rows:
        db.execmany("INSERT OR REPLACE INTO holidays VALUES (?,?)", rows)
    return len(rows)


def is_trading_day(db: DB, d: date) -> bool:
    if d.weekday() >= 5:
        return False
    return not db.q("SELECT 1 FROM holidays WHERE date=?", (d.isoformat(),)).shape[0]


# ------------------------------------------------------------------ daily load
def load_day(db: DB, sess: NSESession, d: date, cfg: dict) -> int:
    """Download NSE (and BSE for BSE-only stocks) bhavcopy for d. Raises NotPublished."""
    series = tuple(cfg["data"]["series"])
    nse = fetch_nse_bhavcopy(sess, d, series)
    if nse.empty:
        raise NotPublished(f"NSE bhavcopy for {d} is empty")
    db.upsert_prices(nse, "NSE")
    n = len(nse)
    bse_isins = set(db.q("SELECT isin FROM universe WHERE active=1 AND exchange='BSE'")["isin"])
    if bse_isins:
        try:
            bse = fetch_bse_bhavcopy(sess, d)
            bse = bse[bse["isin"].isin(bse_isins)]
            db.upsert_prices(bse, "BSE")
            n += len(bse)
        except Exception as e:
            log.warning("BSE bhavcopy for %s failed: %s", d, e)
    db.mark_day_loaded(d, "NSE", n)
    return n


def backfill(db: DB, sess: NSESession, cfg: dict, years: int | None = None, until: date | None = None,
             progress=None) -> dict:
    years = years or cfg["data"]["backfill_years"]
    until = until or date.today()
    start = until - timedelta(days=int(365.25 * years))
    d, loaded, skipped = start, 0, 0
    while d <= until:
        if d.weekday() < 5 and not db.day_loaded(d):
            try:
                load_day(db, sess, d, cfg)
                loaded += 1
            except NotPublished:
                # Weekday with no file in the past = exchange holiday.
                if d < until:
                    db.exec("INSERT OR IGNORE INTO holidays VALUES (?,?)", (d.isoformat(), "no bhavcopy"))
                skipped += 1
            except Exception as e:
                log.warning("Failed %s: %s", d, e)
                skipped += 1
            if progress:
                progress(d, loaded, skipped)
        d += timedelta(days=1)
    return {"loaded": loaded, "skipped": skipped}


# ------------------------------------------------------------------ corporate actions
def refresh_corporate_actions(db: DB, sess: NSESession, start: date, end: date) -> int:
    sym2isin = dict(db.q("SELECT nse_symbol, isin FROM universe").values)
    rows = []
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=360))
        try:
            data = sess.get_json(NSE_CORP_ACTIONS.format(f=cur, t=nxt))
        except Exception as e:
            log.warning("Corporate actions %s..%s failed: %s", cur, nxt, e)
            data = []
        for it in data or []:
            sym = it.get("symbol")
            isin = it.get("isin") or sym2isin.get(sym)
            if not isin or not it.get("exDate") or it.get("exDate") == "-":
                continue
            try:
                ex = datetime.strptime(it["exDate"], "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
            for kind, f in parse_subject(it.get("subject", "")):
                rows.append((isin, sym, ex, kind, f, it.get("subject", "")))
        cur = nxt + timedelta(days=1)
    if rows:
        db.execmany("INSERT OR REPLACE INTO corp_actions VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


def rebuild_adjusted(db: DB, isin: str, symbol: str, source: str = "NSE") -> pd.DataFrame:
    raw = db.raw_prices(isin, source)
    adj = adjust_prices(raw, db.corp_actions(isin))
    db.save_adj_prices(symbol, adj)
    return adj


# ------------------------------------------------------------------ results dates
def refresh_results_dates(db: DB, sess: NSESession, start: date, end: date) -> int:
    rows = []
    try:
        data = sess.get_json(NSE_BOARD_MEETINGS.format(f=start, t=end))
    except Exception as e:
        log.warning("Board meetings download failed: %s", e)
        return 0
    for it in data or []:
        purpose = (it.get("bm_purpose") or "") + " " + (it.get("bm_desc") or "")
        if "result" not in purpose.lower():
            continue
        try:
            d = datetime.strptime(it["bm_date"], "%d-%b-%Y").date().isoformat()
        except (KeyError, ValueError):
            continue
        rows.append((it.get("bm_symbol"), d, "NSE"))
    if rows:
        db.execmany("INSERT OR IGNORE INTO results_dates VALUES (?,?,?)", rows)
    return len(rows)
