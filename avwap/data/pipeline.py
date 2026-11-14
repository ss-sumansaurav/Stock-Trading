"""Data pipeline: universe, holidays, daily bhavcopy, backfill, corporate actions, results dates."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import pandas as pd

from ..db import DB
from .corporate_actions import adjust_prices, parse_subject
from .nse import (NSE_BOARD_MEETINGS, fetch_bse_code_map, NSE_CORP_ACTIONS, NSE_HOLIDAYS, NotPublished, NSESession,
                  fetch_bse_bhavcopy, fetch_nse_bhavcopy, fetch_nse_equity_list)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ universe
def _pick(cols, *names, exclude=()):
    low = {str(c).lower().strip(): c for c in cols}
    for n in names:
        for lc, c in low.items():
            if n in lc and not any(x in lc for x in exclude):
                return c
    return None


def _read_table(path: str) -> pd.DataFrame:
    """Read CSV/Excel and find the real header row (BSE downloads sometimes have title rows)."""
    if path.lower().endswith((".xls", ".xlsx")):
        raw = pd.read_excel(path, header=None, dtype=str)
    else:
        import csv
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
        width = max((len(r) for r in rows), default=0)
        raw = pd.DataFrame([r + [None] * (width - len(r)) for r in rows])
    for i in range(min(len(raw), 20)):
        cells = [str(x).lower() for x in raw.iloc[i].tolist()]
        if any("isin" in c or "code" in c for c in cells):
            df = raw.iloc[i + 1:].copy()
            df.columns = [str(x).strip() for x in raw.iloc[i].tolist()]
            return df.dropna(how="all").reset_index(drop=True)
    raise ValueError("Couldn't find a header row with a scrip code or ISIN column in " + path)


def _clean(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.replace(r"\.0$", "", regex=True).replace({"nan": "", "None": ""})


def import_universe(db: DB, csv_path: str, sess: NSESession | None = None) -> dict:
    """Load the BSE 500 constituents file downloaded from bseindia.com (index page, code 17).

    Works with a scrip-code column (the BSE download) or an ISIN column. Scrip codes are
    mapped to ISINs with the latest BSE bhavcopy, and ISINs to NSE symbols with NSE's
    EQUITY_L.csv. Keeps history of additions/removals.
    """
    src = _read_table(csv_path)
    c_isin = _pick(src.columns, "isin")
    c_code = _pick(src.columns, "scrip code", "security code", "scripcode", "code", exclude=("isin",))
    c_name = _pick(src.columns, "company", "scrip name", "security name", "name", exclude=("code", "isin", "id"))
    c_sec = _pick(src.columns, "sector", "industry")
    c_sym = _pick(src.columns, "nse symbol", "symbol", "scrip id", "security id", "ticker", exclude=("code",))
    if not c_isin and not c_code:
        raise ValueError(f"Universe file needs a scrip code or ISIN column; found {list(src.columns)}")
    new = pd.DataFrame({
        "isin": _clean(src[c_isin]) if c_isin else "",
        "company": _clean(src[c_name]) if c_name else "",
        "bse_code": _clean(src[c_code]) if c_code else "",
        "sector": _clean(src[c_sec]) if c_sec else "",
        "nse_symbol": _clean(src[c_sym]).str.upper() if c_sym else "",
    })
    new = new[(new["isin"] != "") | (new["bse_code"].str.fullmatch(r"\d+"))].reset_index(drop=True)

    if (new["isin"] == "").any():
        if sess is None:
            raise ValueError("File has no ISINs; a network session is needed to map scrip codes")
        bm = fetch_bse_code_map(sess)
        by_code = dict(zip(bm["bse_code"], bm["isin"]))
        by_id = dict(zip(bm["bse_id"], bm["isin"]))
        miss = new["isin"] == ""
        new.loc[miss, "isin"] = new.loc[miss, "bse_code"].map(by_code).fillna("")
        miss = new["isin"] == ""
        if c_sym:
            new.loc[miss, "isin"] = new.loc[miss, "nse_symbol"].map(by_id).fillna("")
        unmapped = new[new["isin"] == ""]
        for r in unmapped.itertuples():
            log.warning("No ISIN found for scrip code %s (%s); skipped", r.bse_code, r.company)
        new = new[new["isin"] != ""]
    if not c_code:
        new["bse_code"] = ""
    new = new.drop_duplicates("isin")

    nse = None
    if sess is not None:
        try:
            nse = fetch_nse_equity_list(sess)
        except Exception as e:  # network optional
            log.warning("Could not fetch NSE equity list (%s); using symbols from the file", e)
    if nse is not None:
        m = dict(zip(nse["isin"], nse["nse_symbol"]))
        new["nse_symbol"] = new["isin"].map(m).fillna("")
        new["exchange"] = new["isin"].map(lambda i: "NSE" if i in m else "BSE")
    else:
        new["exchange"] = new["nse_symbol"].map(lambda s: "NSE" if s else "BSE")
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
