"""Daily job (sections 2.2, 7.1, 9.4, 9.6).

19:00 IST: try to download today's bhavcopy; retry every 30 minutes until 22:00.
If it never appears: error alert, no scan (never a wrong scan). Non-trading days: nothing.
"""
from __future__ import annotations

import logging
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .alerts import telegram
from .data.nse import NotPublished, NSESession
from .data.pipeline import (is_trading_day, load_day, refresh_corporate_actions,
                            refresh_holidays, refresh_results_dates)
from .db import DB, open_db
from .scan import run_scan

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def log_run(db: DB, d: date, status: str, msg: str, started: datetime):
    db.exec("INSERT INTO runs (trade_date, status, message, started_at, finished_at) VALUES (?,?,?,?,?)",
            (d.isoformat(), status, msg, started.isoformat(timespec="seconds"),
             now_ist().isoformat(timespec="seconds")))


def daily_job(cfg: dict, d: date | None = None, wait: bool = True, send: bool = True) -> str:
    """Run the whole evening pipeline for trading day d (default: today, IST)."""
    started = now_ist()
    d = d or started.date()
    db = open_db(cfg)
    sess = NSESession(cfg["data"]["request_timeout"])
    try:
        refresh_holidays(db, sess, cfg["data"].get("extra_holidays", []))
        if not is_trading_day(db, d):
            log.info("%s is not a trading day; nothing to do", d)
            log_run(db, d, "holiday", "non-trading day", started)
            return "holiday"

        # --- download with retries
        gh, gm = _hm(cfg["data"]["give_up_time"])
        give_up = datetime.combine(d, datetime.min.time(), IST).replace(hour=gh, minute=gm)
        step = timedelta(minutes=cfg["data"]["retry_every_minutes"])
        while not db.day_loaded(d):
            try:
                n = load_day(db, sess, d, cfg)
                log.info("Loaded %s rows for %s", n, d)
            except NotPublished:
                nxt = now_ist() + step
                if not wait or nxt > give_up:
                    msg = f"NSE bhavcopy for {d:%d %b %Y} not published by {cfg['data']['give_up_time']} IST. No scan was run."
                    log.error(msg)
                    log_run(db, d, "missing", msg, started)
                    if send:
                        telegram.send_error(msg)
                    return "missing"
                log.info("Bhavcopy not yet published; retrying at %s", nxt.strftime("%H:%M"))
                time.sleep(step.total_seconds())

        # --- refresh reference data (best effort), then scan
        refresh_corporate_actions(db, sess, d - timedelta(days=10), d + timedelta(days=30))
        refresh_results_dates(db, sess, d - timedelta(days=7), d + timedelta(days=1))
        report = run_scan(db, cfg, d)
        if send:
            telegram.send(telegram.build_summary(report, cfg))
        db.backup(cfg["paths"]["backup_dir"])
        log_run(db, d, "ok", f"{len(report['results'])} stocks in {report['seconds']}s", started)
        return "ok"
    except Exception as e:
        tb = traceback.format_exc()
        log.error("Daily job failed: %s", tb)
        log_run(db, d, "error", str(e), started)
        if send:
            telegram.send_error(f"Daily run for {d:%d %b %Y} failed: {e}")
        return "error"


def start_scheduler(cfg: dict):
    """Blocking scheduler: weekdays at data.download_time IST, plus a monthly universe reminder."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    h, m = _hm(cfg["data"]["download_time"])
    sch = BlockingScheduler(timezone=IST)
    sch.add_job(lambda: daily_job(cfg), "cron", day_of_week="mon-fri", hour=h, minute=m,
                id="daily", misfire_grace_time=3600, coalesce=True)
    sch.add_job(lambda: monthly_universe(cfg), "cron", day=1, hour=9, minute=0, id="universe_refresh")
    log.info("Scheduler started: daily scan at %02d:%02d IST on weekdays", h, m)
    sch.start()


def monthly_universe(cfg: dict):
    """Refresh the BSE 500 list from bseindia.com; ask for a manual import if that fails."""
    from .data.bse_api import fetch_bse500
    from .data.pipeline import import_universe
    try:
        fetch_bse500(cfg["paths"]["universe_csv"])
        res = import_universe(open_db(cfg), cfg["paths"]["universe_csv"], NSESession())
        telegram.send(f"🗓️ BSE 500 list refreshed: {res['total']} stocks "
                      f"({res['added']} added, {res['removed']} removed).")
    except Exception as e:
        telegram.send_error(f"Monthly BSE 500 refresh failed ({e}). Run: python3 -m avwap universe")
