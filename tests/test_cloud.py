import sqlite3
from datetime import date

import pytest

from avwap import cloud
from avwap.db import DB
from avwap.demo import load_demo


@pytest.fixture
def fake_github(monkeypatch):
    store = {}
    monkeypatch.setenv("APP_PASSWORD", "s3cret")
    monkeypatch.setenv("GITHUB_REPO", "me/repo")
    monkeypatch.setattr(cloud, "download", lambda name: store.get(name))
    monkeypatch.setattr(cloud, "upload", lambda name, data: store.__setitem__(name, data))
    return store


def test_encrypt_roundtrip_and_wrong_password(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "a")
    blob = cloud.encrypt(b"hello" * 1000)
    assert b"hello" not in blob and cloud.decrypt(blob) == b"hello" * 1000
    monkeypatch.setenv("APP_PASSWORD", "b")
    with pytest.raises(RuntimeError, match="APP_PASSWORD"):
        cloud.decrypt(blob)


def test_push_pull_and_user_state(fake_github, tmp_path, cfg):
    from avwap.scan import run_scan
    src = tmp_path / "mac.sqlite"
    db = DB(str(src))
    load_demo(db, n=5, days=200)
    run_scan(db, cfg)
    db.exec("INSERT INTO pins VALUES ('DEMO03','2026-01-05','Manual')")
    db.conn.commit()
    cloud.push_db(str(src))
    assert set(fake_github) == {cloud.DB_ASSET, cloud.DASH_ASSET, cloud.STATE_ASSET}
    assert len(fake_github[cloud.DASH_ASSET]) < len(fake_github[cloud.DB_ASSET])

    # dashboard copy has no raw prices but keeps adjusted prices and the pin
    dash = tmp_path / "dash.sqlite"
    assert cloud.pull_db(str(dash), cloud.DASH_ASSET)
    c = sqlite3.connect(dash)
    assert c.execute("SELECT COUNT(*) FROM prices_raw").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM prices_adj").fetchone()[0] > 0
    # a position added on the dashboard reaches the next Actions run
    c.execute("INSERT INTO positions (symbol, entry_date, entry_price, qty) VALUES ('DEMO04','2026-06-01',100,5)")
    c.commit()
    cloud.push_user_state(c)
    act = tmp_path / "actions.sqlite"
    assert cloud.pull_db(str(act))
    a = sqlite3.connect(act)
    assert a.execute("SELECT COUNT(*) FROM positions WHERE symbol='DEMO04'").fetchone()[0] == 1
    assert a.execute("SELECT anchor_date FROM pins WHERE symbol='DEMO03'").fetchone()[0] == "2026-01-05"
    assert a.execute("SELECT COUNT(*) FROM prices_raw").fetchone()[0] > 0


def test_cloud_attempt_before_deadline_is_silent(monkeypatch, tmp_path, cfg):
    from datetime import datetime
    from avwap import runner
    from avwap.data import nse
    cfg = {**cfg, "paths": {**cfg["paths"], "db": str(tmp_path / "t.sqlite")}}
    monkeypatch.setattr(runner, "refresh_holidays", lambda *a, **k: 0)
    monkeypatch.setattr(runner, "load_day", lambda *a, **k: (_ for _ in ()).throw(nse.NotPublished("x")))
    sent = []
    monkeypatch.setattr(runner.telegram, "send_error", lambda m: sent.append(m))
    monkeypatch.setattr(runner, "now_ist", lambda: datetime(2026, 11, 13, 19, 30, tzinfo=runner.IST))
    assert runner.daily_job(cfg, date(2026, 11, 13), wait=False) == "not_yet" and not sent
    monkeypatch.setattr(runner, "now_ist", lambda: datetime(2026, 11, 13, 22, 1, tzinfo=runner.IST))
    assert runner.daily_job(cfg, date(2026, 11, 13), wait=False) == "missing" and len(sent) == 1
    runner.daily_job(cfg, date(2026, 11, 13), wait=False)
    assert len(sent) == 1  # no repeat alert


def test_blocked_message():
    import requests
    from avwap.runner import _describe
    assert "blocks cloud servers" in _describe(requests.HTTPError("403 Client Error: Forbidden"))
