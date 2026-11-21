"""Telegram summary and error alerts (section 7.1), via the Telegram Bot API.

Credentials come from TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (put them in .env).
"""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from urllib.parse import quote

import requests

from ..config import telegram_credentials
from ..engine.signals import NAMES

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"
LIMIT = 4000  # Telegram max is 4096 chars per message

SIGNAL_ORDER = ["cluster_reclaim", "primary_reclaim", "all_above", "pullback_hold",
                "primary_loss", "extension", "supply_test"]


def _fmt(x, nd=2):
    return "–" if x is None or x != x else f"{x:,.{nd}f}"


def chart_link(base: str, symbol: str) -> str:
    return f"{base.rstrip('/')}/?symbol={quote(symbol)}"


def build_summary(report: dict, cfg: dict) -> str:
    ac = cfg["alerts"]
    base = ac["dashboard_url"]
    maxn = ac["max_lines_per_section"]
    e = html.escape
    lines = [f"<b>AVWAP scan · {report['date']}</b>",
             f"{len(report['results'])} stocks scanned in {report['seconds']}s"]

    # Telegram rejects localhost links, so only link when a reachable dashboard URL is set.
    linkable = base and not any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))

    def sym(s):
        return f'<a href="{chart_link(base, s)}">{e(s)}</a>' if linkable else f"<b>{e(s)}</b>"

    # 1. positions
    pos = report.get("positions") or []
    if pos:
        lines += ["", "<b>📌 My positions</b>"]
        for p in pos:
            flag = "⚠️ " if p["alerts"] else ""
            lines.append(
                f"{flag}{sym(p['symbol'])} {_fmt(p['close'])} · P/L {p['profit_pct']:+.1f}% · "
                f"trail {_fmt(p['trailing_avwap'])} ({p['dist_trailing_pct']:+.1f}%) · "
                f"entry AVWAP {_fmt(p['entry_avwap'])} ({p['dist_entry_pct']:+.1f}%)")
            for a in p["alerts"]:
                lines.append(f"   ↳ {e(a)}")

    # 2. 4-AVWAP setup: stocks that closed above all four AVWAPs for the first time
    fours = [s for s in report["results"] if (s.get("four") or {}).get("above")]
    new4 = [s for s in fours if s["four"].get("crossed_today")]
    if fours:
        lines += ["", f"<b>🚀 Above all 4 AVWAPs: {len(new4)} new today</b> ({len(fours)} in total)"]
        new4.sort(key=lambda s: -(s["vol_ratio"] if s["vol_ratio"] == s["vol_ratio"] else 0))
        for s in new4[:maxn]:
            f = s["four"]
            vol = f"vol {s['vol_ratio']:.1f}×" if s["vol_ratio"] == s["vol_ratio"] else "vol –"
            part = "" if f.get("complete") else f" ({4 - len(f['missing'])}/4)"
            lines.append(f"{sym(s['symbol'])} {_fmt(s['close'])} · highest AVWAP {_fmt(f['max_avwap'])} "
                         f"({f['dist_pct']:+.1f}%){part} · {vol}")
        if len(new4) > maxn:
            lines.append(f"… and {len(new4) - maxn} more (see the 4-AVWAP tab)")

    # 3. signals grouped by type
    by = defaultdict(list)
    for s in report["results"]:
        for x in s["signals"]:
            by[x["signal"]].append((s, x))
    if by:
        lines += ["", "<b>🔔 New signals</b>"]
    else:
        lines += ["", "No new signals today."]
    for sig in SIGNAL_ORDER:
        items = by.get(sig)
        if not items:
            continue
        items.sort(key=lambda t: (not t[1]["vol_confirmed"], -(t[0]["vol_ratio"] or 0)))
        lines.append(f"\n<u>{NAMES[sig]}</u> ({len(items)})")
        for s, x in items[:maxn]:
            vol = f"vol {s['vol_ratio']:.1f}×" if s["vol_ratio"] == s["vol_ratio"] else "vol –"
            strong = " 💪" if x["vol_confirmed"] else ""
            bse = " [BSE]" if s.get("exchange") == "BSE" else ""
            lines.append(
                f"{sym(s['symbol'])}{bse} {_fmt(s['close'])} · primary {_fmt(s['primary_avwap'])} "
                f"({s['primary_dist_pct']:+.1f}%) · {vol}{strong}")
        if len(items) > maxn:
            lines.append(f"… and {len(items) - maxn} more (see dashboard)")

    # 4. top clusters: tightest clusters with most members, closest to price
    cl = []
    for s in report["results"]:
        for c in s["clusters"]:
            cl.append((s, c))
    cl.sort(key=lambda t: (-len(t[1]["members"]), abs(t[1].get("dist_pct", 99))))
    if cl:
        lines += ["", "<b>🧱 Top clusters</b>"]
        for s, c in cl[: ac["top_clusters"]]:
            lines.append(
                f"{sym(s['symbol'])} {_fmt(s['close'])} · {c['position']} "
                f"{_fmt(c['low'])}–{_fmt(c['high'])} ({len(c['members'])} AVWAPs, {c.get('dist_pct', 0):+.1f}%)")
    if report.get("errors"):
        lines += ["", f"⚠️ {len(report['errors'])} stocks failed to scan (see logs)."]
    lines += ["", "<i>Levels and conditions only, not recommendations.</i>"]
    return "\n".join(lines)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += (line + "\n")
    if cur.strip():
        parts.append(cur)
    return parts


def send(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    if token is None or chat_id is None:
        t2, c2 = telegram_credentials()
        token, chat_id = token or t2, chat_id or c2
    if not token or not chat_id:
        log.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID); message:\n%s", text)
        return False
    ok = True
    for part in split_message(text):
        r = requests.post(API.format(token=token), timeout=30, data={
            "chat_id": chat_id, "text": part, "parse_mode": "HTML",
            "disable_web_page_preview": "true"})
        if r.status_code != 200:
            log.error("Telegram send failed: %s %s", r.status_code, r.text[:300])
            ok = False
    return ok


def send_error(msg: str) -> bool:
    return send(f"❌ <b>AVWAP scanner error</b>\n{html.escape(msg)}")
