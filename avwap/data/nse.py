"""HTTP access to NSE / BSE public files.

NSE blocks requests without browser-like headers and, for /api/ endpoints, without the
cookies set by the home page. NSESession handles both and retries politely.
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date

import pandas as pd
import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

NSE_UDIFF = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
NSE_LEGACY = ("https://nsearchives.nseindia.com/content/historical/EQUITIES/{d:%Y}/{mon}/"
              "cm{d:%d}{mon}{d:%Y}bhav.csv.zip")
BSE_UDIFF = "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV"
NSE_EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_HOLIDAYS = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_CORP_ACTIONS = ("https://www.nseindia.com/api/corporates-corporateActions?index=equities"
                    "&from_date={f:%d-%m-%Y}&to_date={t:%d-%m-%Y}")
NSE_BOARD_MEETINGS = ("https://www.nseindia.com/api/corporate-board-meetings?index=equities"
                      "&from_date={f:%d-%m-%Y}&to_date={t:%d-%m-%Y}")
UDIFF_START = date(2024, 7, 8)  # NSE switched to the UDiFF bhavcopy format on this date


class NotPublished(Exception):
    """File is not (yet) available: 404 or empty."""


class NSESession:
    def __init__(self, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "*/*",
                               "Accept-Language": "en-US,en;q=0.9",
                               "Referer": "https://www.nseindia.com/"})
        self.timeout = timeout
        self._warm = False

    def _warmup(self):
        if not self._warm:
            try:
                self.s.get("https://www.nseindia.com/", timeout=self.timeout)
            except requests.RequestException as e:  # pragma: no cover - network
                log.warning("NSE warm-up failed: %s", e)
            self._warm = True

    def get(self, url: str, api: bool = False, tries: int = 3) -> bytes:
        if api:
            self._warmup()
        last = None
        for k in range(tries):
            try:
                r = self.s.get(url, timeout=self.timeout)
                if r.status_code == 404:
                    raise NotPublished(url)
                if r.status_code in (401, 403) and api:
                    self._warm = False
                    self._warmup()
                    raise requests.HTTPError(f"{r.status_code} for {url}")
                r.raise_for_status()
                if not r.content:
                    raise NotPublished(url)
                return r.content
            except NotPublished:
                raise
            except requests.RequestException as e:
                last = e
                time.sleep(2 * (k + 1))
        raise last  # type: ignore[misc]

    def get_json(self, url: str):
        import json
        return json.loads(self.get(url, api=True))


def _read_csv_maybe_zip(content: bytes) -> pd.DataFrame:
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
            content = z.read(name)
    df = pd.read_csv(io.BytesIO(content))
    df.columns = [c.strip() for c in df.columns]
    return df


def parse_udiff(df: pd.DataFrame, d: date, series=("EQ", "BE")) -> pd.DataFrame:
    df = df[df["SctySrs"].astype(str).str.strip().isin(series)]
    return pd.DataFrame({
        "isin": df["ISIN"].astype(str).str.strip(),
        "date": d.isoformat(),
        "symbol": df["TckrSymb"].astype(str).str.strip(),
        "open": df["OpnPric"].astype(float), "high": df["HghPric"].astype(float),
        "low": df["LwPric"].astype(float), "close": df["ClsPric"].astype(float),
        "volume": df["TtlTradgVol"].astype(float),
        "series": df["SctySrs"].astype(str).str.strip(),
    })


def parse_legacy(df: pd.DataFrame, d: date, series=("EQ", "BE")) -> pd.DataFrame:
    df = df[df["SERIES"].astype(str).str.strip().isin(series)]
    return pd.DataFrame({
        "isin": df["ISIN"].astype(str).str.strip(),
        "date": d.isoformat(),
        "symbol": df["SYMBOL"].astype(str).str.strip(),
        "open": df["OPEN"].astype(float), "high": df["HIGH"].astype(float),
        "low": df["LOW"].astype(float), "close": df["CLOSE"].astype(float),
        "volume": df["TOTTRDQTY"].astype(float),
        "series": df["SERIES"].astype(str).str.strip(),
    })


def fetch_nse_bhavcopy(sess: NSESession, d: date, series=("EQ", "BE")) -> pd.DataFrame:
    if d >= UDIFF_START:
        return parse_udiff(_read_csv_maybe_zip(sess.get(NSE_UDIFF.format(d=d))), d, series)
    mon = d.strftime("%b").upper()
    return parse_legacy(_read_csv_maybe_zip(sess.get(NSE_LEGACY.format(d=d, mon=mon))), d, series)


def fetch_bse_bhavcopy(sess: NSESession, d: date) -> pd.DataFrame:
    """BSE UDiFF bhavcopy (same column names as NSE's). Used for stocks not on NSE."""
    raw = _read_csv_maybe_zip(sess.get(BSE_UDIFF.format(d=d)))
    keep = ("A", "B", "T", "X", "XT", "Z", "M", "MT", "P", "ZP", "R", "MS", "W", "E")
    return parse_udiff(raw, d, keep)


def fetch_nse_equity_list(sess: NSESession) -> pd.DataFrame:
    df = _read_csv_maybe_zip(sess.get(NSE_EQUITY_LIST))
    return pd.DataFrame({"nse_symbol": df["SYMBOL"].str.strip(),
                         "isin": df["ISIN NUMBER"].str.strip(),
                         "series": df["SERIES"].str.strip()})
