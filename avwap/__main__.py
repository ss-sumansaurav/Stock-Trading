"""Command line: python -m avwap <command>

  init                         create the database
  universe FILE                import BSE 500 constituents (CSV/XLSX with ISIN column)
  backfill [--years N]         download N years of NSE bhavcopies (+ corporate actions)
  scan [--date YYYY-MM-DD]     run the scan on data already loaded
  daily [--date D] [--no-wait] full evening job: download, scan, Telegram, backup
  schedule                     run forever, doing `daily` each weekday at 19:00 IST
  demo                         load synthetic demo data (use a separate --settings/db)
  telegram-test                send a test message to your Telegram channel
  position add SYM DATE PRICE QTY | position list | position close ID
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from .config import load_settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="avwap", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", help="path to settings.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    u = sub.add_parser("universe"); u.add_argument("file")
    b = sub.add_parser("backfill"); b.add_argument("--years", type=float)
    s = sub.add_parser("scan"); s.add_argument("--date"); s.add_argument("--send", action="store_true")
    d = sub.add_parser("daily"); d.add_argument("--date"); d.add_argument("--no-wait", action="store_true")
    d.add_argument("--no-send", action="store_true")
    sub.add_parser("schedule")
    sub.add_parser("telegram-test")
    sub.add_parser("demo")
    p = sub.add_parser("position"); p.add_argument("action", choices=["add", "list", "close"])
    p.add_argument("args", nargs="*")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_settings(a.settings)

    from .db import open_db
    db = open_db(cfg)

    if a.cmd == "init":
        print("Database ready at", cfg["paths"]["db"])
    elif a.cmd == "universe":
        from .data.nse import NSESession
        from .data.pipeline import import_universe
        print(import_universe(db, a.file, NSESession()))
    elif a.cmd == "backfill":
        from .data.nse import NSESession
        from .data.pipeline import backfill, refresh_corporate_actions, refresh_holidays, refresh_results_dates
        sess = NSESession(cfg["data"]["request_timeout"])
        refresh_holidays(db, sess, cfg["data"].get("extra_holidays", []))
        yrs = a.years or cfg["data"]["backfill_years"]
        res = backfill(db, sess, cfg, yrs, progress=lambda d, l, s: print(f"\r{d} loaded={l} skipped={s}", end=""))
        print("\n", res)
        start = date.today() - timedelta(days=int(365.25 * yrs))
        print("corporate actions:", refresh_corporate_actions(db, sess, start, date.today()))
        print("results dates:", refresh_results_dates(db, sess, date.today() - timedelta(days=400), date.today()))
    elif a.cmd == "scan":
        from .scan import run_scan
        rep = run_scan(db, cfg, date.fromisoformat(a.date) if a.date else None)
        nsig = sum(len(r["signals"]) for r in rep["results"])
        print(f"{rep['date']}: {len(rep['results'])} stocks, {nsig} signals, {rep['seconds']}s")
        if a.send:
            from .alerts import telegram
            telegram.send(telegram.build_summary(rep, cfg))
    elif a.cmd == "daily":
        from .runner import daily_job
        print(daily_job(cfg, date.fromisoformat(a.date) if a.date else None,
                        wait=not a.no_wait, send=not a.no_send))
    elif a.cmd == "schedule":
        from .runner import start_scheduler
        start_scheduler(cfg)
    elif a.cmd == "demo":
        from .demo import load_demo
        from .scan import run_scan
        load_demo(db)
        rep = run_scan(db, cfg)
        print(f"demo loaded: {len(rep['results'])} stocks scanned in {rep['seconds']}s")
    elif a.cmd == "telegram-test":
        from .alerts import telegram
        ok = telegram.send("✅ AVWAP scanner is connected to this channel.")
        print("sent" if ok else "NOT sent: check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env")
        return 0 if ok else 1
    elif a.cmd == "position":
        if a.action == "add":
            sym, d_, px, qty = a.args
            db.exec("INSERT INTO positions (symbol, entry_date, entry_price, qty) VALUES (?,?,?,?)",
                    (sym.upper(), d_, float(px), float(qty)))
            print("added")
        elif a.action == "list":
            print(db.q("SELECT * FROM positions WHERE closed=0").to_string(index=False))
        else:
            db.exec("UPDATE positions SET closed=1 WHERE id=?", (int(a.args[0]),))
            print("closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
