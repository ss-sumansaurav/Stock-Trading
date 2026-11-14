"""Fetch the BSE 500 constituents from BSE's public website API (no manual download).

BSE has no single "constituents" endpoint, so this combines two calls the bseindia.com pages use:
- Market gainers/losers for all stocks: each row lists the indices the stock belongs to
  (`index_code`), so filtering for "BSE 500" gives the members. Both calls are also asked
  for the BSE 500 index directly, and the results are merged.
- The list of securities, which gives ISIN, scrip ID and industry for each scrip code.

A stock that closed exactly unchanged on the day appears in neither gainers nor losers, so a
handful may be missed; the importer prints the count so you can check it is close to 500.
"""
from __future__ import annotations

import logging
import re
import time

import pandas as pd
import requests

from .nse import UA

log = logging.getLogger(__name__)
API = "https://api.bseindia.com/BseIndiaAPI/api"
INDEX_NAMES = ("BSE 500", "S&P BSE 500")
_IN_500 = re.compile(r"\|\s*(?:S&P\s+)?BSE\s*500\s*\|", re.I)
GROUPS = ("A", "B", "T", "X", "XT", "M", "MT", "Z", "ZP", "P", "R", "W", "E")


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                      "Accept-Language": "en-US,en;q=0.5",
                      "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"})
    return s


def _get(s, path, params, tries=3):
    last = None
    for k in range(tries):
        try:
            r = s.get(f"{API}/{path}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e
            time.sleep(1.5 * (k + 1))
    raise RuntimeError(f"BSE API {path} failed: {last}")


def _rows(js):
    if isinstance(js, dict):
        js = js.get("Table") or js.get("Table1") or []
    return js or []


def bse500_members(s) -> dict[str, str]:
    """scrip code -> name, for stocks that are in the BSE 500."""
    out: dict[str, str] = {}
    for gl in ("gainer", "loser"):
        try:
            for r in _rows(_get(s, "MktRGainerLoserData/w",
                                {"GLtype": gl, "IndxGrp": "all", "IndxGrpval": "", "orderby": "all"})):
                if _IN_500.search(str(r.get("index_code", ""))):
                    out[str(r["scrip_cd"])] = r.get("LONG_NAME") or r.get("scripname", "")
        except RuntimeError as e:
            log.warning("%s", e)
        for name in INDEX_NAMES:
            try:
                for r in _rows(_get(s, "MktRGainerLoserData/w",
                                    {"GLtype": gl, "IndxGrp": "index", "IndxGrpval": name, "orderby": "all"})):
                    out[str(r["scrip_cd"])] = r.get("LONG_NAME") or r.get("scripname", "")
            except RuntimeError as e:
                log.warning("%s", e)
    return out


def security_details(s, codes) -> pd.DataFrame:
    """ISIN, scrip id and industry for the given scrip codes."""
    want, rows = set(codes), []
    for g in GROUPS:
        if not want - {r["Scrip Code"] for r in rows}:
            break
        try:
            js = _get(s, "ListofScripData/w", {"scripcode": "", "Group": g, "industry": "",
                                               "segment": "Equity", "status": "Active"})
        except RuntimeError as e:
            log.warning("%s", e)
            continue
        for r in _rows(js):
            code = str(r.get("SCRIP_CD", "")).strip()
            if code in want:
                rows.append({"Scrip Code": code, "Company": r.get("Scrip_Name") or r.get("Issuer_Name"),
                             "Scrip ID": r.get("scrip_id"), "ISIN": r.get("ISIN_NUMBER"),
                             "Industry": r.get("INDUSTRY")})
    return pd.DataFrame(rows).drop_duplicates("Scrip Code") if rows else pd.DataFrame(
        columns=["Scrip Code", "Company", "Scrip ID", "ISIN", "Industry"])


def fetch_bse500(out_csv: str) -> pd.DataFrame:
    """Download the BSE 500 list and save it as a CSV the universe importer reads."""
    s = _session()
    members = bse500_members(s)
    if len(members) < 50:
        raise RuntimeError(f"BSE returned only {len(members)} BSE 500 members; the site may be down "
                           "or its API changed. Try again later, or import a downloaded file instead.")
    det = security_details(s, members.keys())
    df = pd.DataFrame({"Scrip Code": list(members), "Company": list(members.values())})
    df = df.merge(det.drop(columns=["Company"]), on="Scrip Code", how="left")
    df.to_csv(out_csv, index=False)
    log.info("Fetched %d BSE 500 members (%d with ISIN) -> %s", len(df), df["ISIN"].notna().sum(), out_csv)
    return df
