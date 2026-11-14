"""Entry point for GitHub Actions: download data, run one step, upload data."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from . import cloud
from .alerts import telegram

log = logging.getLogger(__name__)


def cloud_run(cfg: dict, action: str = "daily", years: float | None = None) -> int:
    db_path = cfg["paths"]["db"]
    have_data = cloud.pull_db(db_path)

    if action == "telegram-test":
        ok = telegram.send("✅ AVWAP scanner on GitHub Actions is connected to this channel.")
        return 0 if ok else 1

    if action == "backfill":
        from .data.nse import NSESession
        from .data.pipeline import (backfill, import_universe, refresh_corporate_actions,
                                    refresh_holidays, refresh_results_dates)
        from .db import open_db
        from .scan import run_scan
        db = open_db(cfg)
        sess = NSESession(cfg["data"]["request_timeout"])
        try:
            if db.active_universe().empty:
                from .data.bse_api import fetch_bse500
                fetch_bse500(cfg["paths"]["universe_csv"])
                print(import_universe(db, cfg["paths"]["universe_csv"], sess))
            refresh_holidays(db, sess, cfg["data"].get("extra_holidays", []))
            yrs = years or cfg["data"]["backfill_years"]
            res = backfill(db, sess, cfg, yrs)
            print("backfill:", res)
            if res["loaded"] == 0 and not have_data:
                raise RuntimeError("No price files could be downloaded from NSE; it is probably "
                                   "blocking GitHub's servers. Upload your Mac's data instead.")
            refresh_corporate_actions(db, sess, date.today() - timedelta(days=int(365.25 * yrs)), date.today())
            refresh_results_dates(db, sess, date.today() - timedelta(days=400), date.today())
            rep = run_scan(db, cfg)
            telegram.send(telegram.build_summary(rep, cfg))
        except Exception as e:
            from .runner import _describe
            telegram.send_error(f"Backfill on GitHub failed: {_describe(e)}")
            db.conn.close()
            cloud.push_db(db_path)
            return 1
        db.conn.close()
        cloud.push_db(db_path)
        return 0

    # daily
    if not have_data:
        log.error("No data stored in GitHub yet. Run `python3 -m avwap cloud-upload` on your Mac, "
                  "or run this workflow once with action 'backfill'.")
        return 1
    from .runner import daily_job
    status = daily_job(cfg, wait=False)
    print("status:", status)
    if status in ("ok", "missing", "error"):
        cloud.push_db(db_path)
    return 1 if status == "error" else 0
