from datetime import date

import pandas as pd
import pytest

from avwap.alerts import telegram
from avwap.data import nse
from avwap.data.nse import parse_udiff
from avwap.demo import load_demo
from avwap.scan import run_scan


def test_parse_udiff_filters_series():
    raw = pd.DataFrame({
        "TradDt": ["2026-11-13"] * 3, "ISIN": ["INE1", "INE2", "INE3"], "TckrSymb": ["AAA", "BBB", "CCC"],
        "SctySrs": ["EQ", "BE", "N1"], "OpnPric": [1, 2, 3], "HghPric": [2, 3, 4], "LwPric": [0.5, 1, 2],
        "ClsPric": [1.5, 2.5, 3.5], "TtlTradgVol": [100, 200, 300]})
    out = parse_udiff(raw, date(2026, 11, 13))
    assert out["symbol"].tolist() == ["AAA", "BBB"]
    assert out["volume"].tolist() == [100, 200]


def test_demo_scan_positions_and_summary(db, cfg):
    load_demo(db, n=12, days=420)
    rep = run_scan(db, cfg)
    assert len(rep["results"]) == 12
    assert rep["positions"], "demo positions should be evaluated"
    # the split stock (DEMO01) has no jump after adjustment
    adj = db.adj_prices("DEMO01")
    assert (adj["close"].pct_change().abs().max()) < 0.2
    msg = telegram.build_summary(rep, cfg)
    assert "My positions" in msg and msg.index("My positions") < msg.find("signals")
    assert "<b>DEMO" in msg  # localhost dashboard: no links
    cfg2 = {**cfg, "alerts": {**cfg["alerts"], "dashboard_url": "https://scanner.example.com"}}
    assert "https://scanner.example.com/?symbol=DEMO" in telegram.build_summary(rep, cfg2)
    for part in telegram.split_message(msg * 5):
        assert len(part) <= 4096


def test_missing_bhavcopy_sends_error_and_no_scan(monkeypatch, tmp_path, cfg):
    from avwap import runner
    cfg = {**cfg, "paths": {**cfg["paths"], "db": str(tmp_path / "t.sqlite"),
                            "backup_dir": str(tmp_path / "b")}}
    sent = []
    monkeypatch.setattr(runner.telegram, "send_error", lambda m: sent.append(m) or True)
    monkeypatch.setattr(runner, "refresh_holidays", lambda *a, **k: 0)

    def not_published(*a, **k):
        raise nse.NotPublished("x")
    monkeypatch.setattr(runner, "load_day", not_published)
    scanned = []
    monkeypatch.setattr(runner, "run_scan", lambda *a, **k: scanned.append(1))
    assert runner.daily_job(cfg, date(2026, 11, 13), wait=False) == "missing"
    assert sent and "not published" in sent[0] and not scanned


def test_holiday_does_nothing(monkeypatch, tmp_path, cfg):
    from avwap import runner
    cfg = {**cfg, "paths": {**cfg["paths"], "db": str(tmp_path / "t.sqlite")}}
    monkeypatch.setattr(runner, "refresh_holidays", lambda db, *a, **k: db.exec(
        "INSERT OR IGNORE INTO holidays VALUES ('2026-11-13','test')"))
    sent = []
    monkeypatch.setattr(runner.telegram, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(runner.telegram, "send_error", lambda *a, **k: sent.append(a))
    assert runner.daily_job(cfg, date(2026, 11, 13)) == "holiday"
    assert runner.daily_job(cfg, date(2026, 11, 14)) == "holiday"  # Saturday
    assert not sent


def test_universe_from_bse_download_without_isin(db, tmp_path, monkeypatch):
    from avwap.data import pipeline
    f = tmp_path / "bse500.csv"
    f.write_text("BSE 500 constituents\n\n"
                 "Scrip Code,Scrip Name,Scrip ID,Industry\n"
                 "532755,TECH MAHINDRA LTD.,TECHM,IT - Software\n"
                 "532210,CITY UNION BANK LTD.,CUB,Banks\n"
                 "530135,OPTIEMUS INFRACOM LTD.,OPTIEMUS,Telecom\n"
                 "999999,BSE ONLY LTD.,BSEONLY,Misc\n")
    monkeypatch.setattr(pipeline, "fetch_bse_code_map", lambda s: pd.DataFrame({
        "bse_code": ["532755", "532210", "530135", "999999"],
        "bse_id": ["TECHM", "CUB", "OPTIEMUS", "BSEONLY"],
        "isin": ["INE669C01036", "INE491A01021", "INE350C01017", "INE000X00000"]}))
    monkeypatch.setattr(pipeline, "fetch_nse_equity_list", lambda s: pd.DataFrame({
        "nse_symbol": ["TECHM", "CUB", "OPTIEMUS"],
        "isin": ["INE669C01036", "INE491A01021", "INE350C01017"], "series": ["EQ"] * 3}))
    res = pipeline.import_universe(db, str(f), sess=object())
    assert res == {"total": 4, "added": 4, "removed": 0, "bse_only": 1}
    u = db.q("SELECT isin, nse_symbol, bse_code, sector, exchange FROM universe ORDER BY bse_code")
    assert u.set_index("bse_code").loc["532755", "nse_symbol"] == "TECHM"
    assert u.set_index("bse_code").loc["999999", "nse_symbol"] == "BSE-999999"
    assert u.set_index("bse_code").loc["532210", "sector"] == "Banks"
