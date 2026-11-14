"""SQLite storage. One file, history is never deleted."""
from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    isin TEXT PRIMARY KEY,
    company TEXT,
    bse_code TEXT,
    nse_symbol TEXT,
    sector TEXT,
    exchange TEXT DEFAULT 'NSE',      -- NSE, or BSE when not listed on NSE
    active INTEGER DEFAULT 1,
    added_on TEXT,
    removed_on TEXT
);
CREATE TABLE IF NOT EXISTS universe_history (
    date TEXT, isin TEXT, company TEXT, action TEXT   -- added / removed
);
CREATE TABLE IF NOT EXISTS prices_raw (            -- exactly as published, keyed by ISIN
    isin TEXT, date TEXT, symbol TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, series TEXT, source TEXT,           -- source: NSE / BSE
    PRIMARY KEY (isin, date, source)
);
CREATE TABLE IF NOT EXISTS prices_adj (
    symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS corp_actions (
    isin TEXT, symbol TEXT, ex_date TEXT, kind TEXT, factor REAL, subject TEXT,
    PRIMARY KEY (isin, ex_date, kind)
);
CREATE TABLE IF NOT EXISTS loaded_days (
    date TEXT PRIMARY KEY, source TEXT, rows INTEGER, loaded_at TEXT
);
CREATE TABLE IF NOT EXISTS holidays (
    date TEXT PRIMARY KEY, description TEXT
);
CREATE TABLE IF NOT EXISTS results_dates (
    symbol TEXT, date TEXT, source TEXT, PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS manual_anchors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, date TEXT, label TEXT,
    action TEXT,                    -- add / delete (delete hides an automatic anchor)
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS pins (
    symbol TEXT PRIMARY KEY, anchor_date TEXT, anchor_label TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, entry_date TEXT, entry_price REAL, qty REAL,
    trailing_anchor_date TEXT,      -- NULL = auto (most recent higher low after entry)
    notes TEXT, closed INTEGER DEFAULT 0,
    last_hl_seen TEXT               -- last higher low we notified about
);
CREATE TABLE IF NOT EXISTS scan_results (
    date TEXT, symbol TEXT, close REAL, primary_label TEXT, primary_date TEXT,
    primary_avwap REAL, primary_dist_pct REAL, above_primary INTEGER,
    major_low_avwap REAL, in_cluster INTEGER, cluster_desc TEXT,
    vol_ratio REAL, signals TEXT, sector TEXT, exchange TEXT, payload TEXT,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS signals (
    date TEXT, symbol TEXT, signal TEXT, detail TEXT, vol_confirmed INTEGER,
    PRIMARY KEY (date, symbol, signal)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT, status TEXT, message TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_prices_raw_date ON prices_raw(date);
CREATE INDEX IF NOT EXISTS ix_prices_raw_isin ON prices_raw(isin, source);
CREATE INDEX IF NOT EXISTS ix_signals_date ON signals(date);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params=()) -> pd.DataFrame:
        return pd.read_sql_query(sql, self.conn, params=params)

    def exec(self, sql: str, params=()):
        with self.tx() as c:
            return c.execute(sql, params)

    def execmany(self, sql: str, rows):
        with self.tx() as c:
            c.executemany(sql, rows)

    # ---------- prices ----------
    def upsert_prices(self, df: pd.DataFrame, source: str):
        """df columns: isin, date, symbol, open, high, low, close, volume, series."""
        rows = [
            (r.isin, r.date, r.symbol, r.open, r.high, r.low, r.close, r.volume, r.series, source)
            for r in df.itertuples(index=False)
        ]
        self.execmany(
            "INSERT OR REPLACE INTO prices_raw VALUES (?,?,?,?,?,?,?,?,?,?)", rows
        )

    def raw_prices(self, isin: str, source: str = "NSE") -> pd.DataFrame:
        df = self.q(
            "SELECT date, open, high, low, close, volume FROM prices_raw "
            "WHERE isin=? AND source=? ORDER BY date", (isin, source)
        )
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def adj_prices(self, symbol: str) -> pd.DataFrame:
        df = self.q(
            "SELECT date, open, high, low, close, volume FROM prices_adj "
            "WHERE symbol=? ORDER BY date", (symbol,)
        )
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def save_adj_prices(self, symbol: str, df: pd.DataFrame):
        with self.tx() as c:
            c.execute("DELETE FROM prices_adj WHERE symbol=?", (symbol,))
            c.executemany(
                "INSERT INTO prices_adj VALUES (?,?,?,?,?,?,?)",
                [
                    (symbol, d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else d,
                     r.open, r.high, r.low, r.close, r.volume)
                    for d, r in zip(df.index, df.itertuples(index=False))
                ],
            )

    def corp_actions(self, isin: str) -> pd.DataFrame:
        return self.q(
            "SELECT ex_date, kind, factor, subject FROM corp_actions WHERE isin=? ORDER BY ex_date",
            (isin,),
        )

    def day_loaded(self, d: date) -> bool:
        return bool(self.q("SELECT 1 FROM loaded_days WHERE date=?", (str(d),)).shape[0])

    def mark_day_loaded(self, d: date, source: str, rows: int):
        self.exec(
            "INSERT OR REPLACE INTO loaded_days VALUES (?,?,?,?)",
            (str(d), source, rows, datetime.now().isoformat(timespec="seconds")),
        )

    def last_loaded_day(self) -> str | None:
        df = self.q("SELECT MAX(date) AS d FROM loaded_days")
        return df["d"].iloc[0] if len(df) else None

    # ---------- universe ----------
    def active_universe(self) -> pd.DataFrame:
        return self.q(
            "SELECT isin, company, nse_symbol, bse_code, sector, exchange FROM universe "
            "WHERE active=1 ORDER BY nse_symbol"
        )

    def backup(self, backup_dir: str):
        if self.path == ":memory:":
            return None
        Path(backup_dir).mkdir(parents=True, exist_ok=True)
        dest = os.path.join(backup_dir, f"avwap-{date.today():%Y%m%d}.sqlite")
        bck = sqlite3.connect(dest)
        with bck:
            self.conn.backup(bck)
        bck.close()
        return dest


def open_db(cfg: dict) -> DB:
    return DB(cfg["paths"]["db"])
