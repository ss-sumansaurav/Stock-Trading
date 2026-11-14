"""AVWAP Scanner dashboard (section 7.2). Run:  streamlit run dashboard/app.py

Works on a phone browser: single-column layout, horizontal page switcher, touch-friendly charts.
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from avwap.config import load_settings  # noqa: E402
from avwap.db import DB  # noqa: E402
from avwap.engine.avwap import anchored_vwap  # noqa: E402
from avwap.engine.positions import evaluate_position  # noqa: E402
from avwap.engine.signals import NAMES  # noqa: E402
from avwap.scan import analyze_symbol  # noqa: E402

st.set_page_config(page_title="AVWAP Scanner", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container {padding-top: 1.2rem; padding-left: 0.8rem; padding-right: 0.8rem; max-width: 1400px;}
div[data-testid="stMetricValue"] {font-size: 1.25rem;}
@media (max-width: 640px) { h1 {font-size: 1.5rem;} }
</style>""", unsafe_allow_html=True)

AUTO_COLORS = ["#2a78d6", "#1aa37a", "#8a5cd6", "#c9a400", "#0fa3b1", "#5e7d99",
               "#7bae3c", "#a35f9c", "#3d5afe", "#00897b", "#9e7d4a", "#607d8b"]
MANUAL_COLORS = ["#ff6b2c", "#e8336f", "#ff9f1c", "#d81b60"]


from avwap import cloud  # noqa: E402


def _secret(name):
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name)


# ---------------------------------------------------------------- password gate
_pw = _secret("APP_PASSWORD")
if _pw and not st.session_state.get("authed"):
    st.markdown("### 📈 AVWAP Scanner")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Open"):
            if hmac.compare_digest(entered.encode(), _pw.encode()):
                st.session_state.authed = True
                st.rerun()
            st.error("Wrong password")
    st.stop()

CLOUD = cloud.enabled() and bool(_secret("GITHUB_TOKEN"))


@st.cache_resource(ttl=1800, show_spinner="Loading the latest scan…")
def get_db(path: str) -> DB:
    if CLOUD:
        local = os.path.join(tempfile.gettempdir(), "avwap-dashboard.sqlite")
        if not cloud.pull_db(local, cloud.DASH_ASSET):
            st.error("No data in GitHub yet. Upload it from your Mac with `python3 -m avwap cloud-upload`.")
            st.stop()
        return DB(local)
    return DB(path)


cfg = load_settings()
db = get_db(cfg["paths"]["db"])


def write(sql: str, params=()):
    """Save a change; in hosted mode also send positions/anchors/pins to GitHub."""
    db.exec(sql, params)
    if CLOUD:
        try:
            cloud.push_user_state(db.conn)
        except Exception as e:
            st.error(f"Saved here, but could not save to GitHub: {e}")

qp = st.query_params
PAGES = ["Scan", "Chart", "Positions", "Status"]
default_page = "Chart" if "symbol" in qp else qp.get("page", "Scan")
if "page" not in st.session_state:
    st.session_state.page = default_page if default_page in PAGES else "Scan"
if "symbol" not in st.session_state and "symbol" in qp:
    st.session_state.symbol = qp["symbol"]

page = st.segmented_control("Page", PAGES, key="page", label_visibility="collapsed") or "Scan"


def latest_scan() -> tuple[str | None, pd.DataFrame]:
    d = db.q("SELECT MAX(date) AS d FROM scan_results")["d"].iat[0]
    if not d:
        return None, pd.DataFrame()
    return d, db.q("SELECT * FROM scan_results WHERE date=?", (d,))


def symbols() -> list[str]:
    return db.q("SELECT nse_symbol FROM universe WHERE active=1 ORDER BY nse_symbol")["nse_symbol"].tolist()


def open_chart(sym: str):
    st.session_state.symbol = sym
    st.session_state.page = "Chart"
    st.query_params["symbol"] = sym


# =============================================================== SCAN
def page_scan():
    d, df = latest_scan()
    st.subheader(f"Scan · {d}" if d else "Scan")
    if df.empty:
        st.info("No scan yet. Run `python -m avwap daily` (or `scan`) first.")
        return
    df["signals"] = df["signals"].fillna("")
    all_sigs = sorted({s.replace(" (vol)", "") for x in df["signals"] for s in x.split(", ") if s})
    with st.expander("Filters", expanded=False):
        c1, c2 = st.columns(2)
        sig = c1.multiselect("Signal", all_sigs)
        sectors = c2.multiselect("Sector", sorted(df["sector"].dropna().unique()))
        lo, hi = float(np.nanmin(df["primary_dist_pct"])), float(np.nanmax(df["primary_dist_pct"]))
        lo, hi = np.floor(lo), np.ceil(hi)
        rng = st.slider("% distance from primary AVWAP", lo, hi, (lo, hi))
        c3, c4, c5 = st.columns(3)
        only_sig = c3.checkbox("Only with signals")
        only_cl = c4.checkbox("Near a cluster")
        side = c5.radio("Price vs primary", ["Any", "Above", "Below"], horizontal=True)
        q = st.text_input("Search symbol")
    f = df.copy()
    if sig:
        f = f[f["signals"].apply(lambda x: any(s in x for s in sig))]
    if sectors:
        f = f[f["sector"].isin(sectors)]
    f = f[(f["primary_dist_pct"].fillna(0) >= rng[0]) & (f["primary_dist_pct"].fillna(0) <= rng[1])]
    if only_sig:
        f = f[f["signals"] != ""]
    if only_cl:
        f = f[f["in_cluster"] == 1]
    if side != "Any":
        f = f[f["above_primary"] == (1 if side == "Above" else 0)]
    if q:
        f = f[f["symbol"].str.contains(q.upper())]

    st.markdown(f"**{len(df)}** stocks · **{int(df['above_primary'].sum())}** above primary · "
                f"**{int((df['signals'] != '').sum())}** with signals · showing **{len(f)}**")

    view = f[["symbol", "close", "signals", "primary_dist_pct", "primary_avwap", "primary_label",
              "vol_ratio", "cluster_desc", "sector", "exchange"]].rename(columns={
        "symbol": "Symbol", "close": "Close", "signals": "Signals", "primary_dist_pct": "% vs primary",
        "primary_avwap": "Primary AVWAP", "primary_label": "Primary anchor", "vol_ratio": "Vol ×20d",
        "cluster_desc": "Clusters", "sector": "Sector", "exchange": "Exch"})
    view = view.sort_values("% vs primary", key=lambda s: s.abs())
    ev = st.dataframe(
        view, hide_index=True, use_container_width=True, height=560,
        on_select="rerun", selection_mode="single-row",
        column_config={
            "Close": st.column_config.NumberColumn(format="%.2f"),
            "Primary AVWAP": st.column_config.NumberColumn(format="%.2f"),
            "% vs primary": st.column_config.NumberColumn(format="%+.2f%%"),
            "Vol ×20d": st.column_config.NumberColumn(format="%.1f×"),
        })
    st.caption("Tap a row to open its chart. Levels and conditions only, not recommendations.")
    rows = ev.selection.rows if ev and hasattr(ev, "selection") else []
    if rows:
        open_chart(view.iloc[rows[0]]["Symbol"])
        st.rerun()


# =============================================================== CHART
def build_figure(an, visible: set, show_ma: bool, months: int):
    df = an.df
    start = df.index[-1] - pd.DateOffset(months=months)
    v = df[df.index >= start]
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.68, 0.16, 0.16],
                        vertical_spacing=0.02)
    fig.add_trace(go.Candlestick(x=v.index, open=v["open"], high=v["high"], low=v["low"], close=v["close"],
                                 name="Price", increasing_line_color="#1aa37a",
                                 decreasing_line_color="#e5484d", showlegend=False), 1, 1)
    # primary bands
    if an.primary is not None:
        b = an.primary_band.loc[v.index]
        for up, dn, op in (("u2", "l2", 0.07), ("u1", "l1", 0.12)):
            fig.add_trace(go.Scatter(x=b.index, y=b[up], line=dict(width=0.6, color="rgba(42,120,214,.5)"),
                                     hoverinfo="skip", showlegend=False), 1, 1)
            fig.add_trace(go.Scatter(x=b.index, y=b[dn], line=dict(width=0.6, color="rgba(42,120,214,.5)"),
                                     fill="tonexty", fillcolor=f"rgba(42,120,214,{op})",
                                     name=f"±{up[1]}σ primary", hoverinfo="skip"), 1, 1)
    # cluster zones
    for c in an.clusters:
        color = "rgba(229,72,77,.2)" if c["position"] == "overhead" else "rgba(26,163,122,.2)"
        fig.add_hrect(y0=c["low"], y1=c["high"], x0=v.index[max(0, len(v) - 40)], x1=v.index[-1],
                      fillcolor=color, line_width=0, row=1, col=1, layer="below")
    # AVWAP lines
    ai, mi = 0, 0
    for a in an.anchors:
        if a.key not in visible:
            continue
        line = an.lines[a.key].loc[v.index]
        if a.source == "manual":
            color = MANUAL_COLORS[mi % len(MANUAL_COLORS)]; mi += 1; dash = "dash"
        else:
            color = AUTO_COLORS[ai % len(AUTO_COLORS)]; ai += 1; dash = "solid"
        is_p = an.primary is not None and a.key == an.primary.key
        width = 3.2 if is_p else 1.3
        name = f"{'★ ' if is_p else ''}{a.label} {a.date:%d-%b-%y}"
        fig.add_trace(go.Scatter(x=line.index, y=line, mode="lines", name=name,
                                 line=dict(color=color, width=width, dash=dash)), 1, 1)
        if a.date >= v.index[0]:
            px = a.price if a.price else df.loc[a.date, "low"]
            is_high = any(k in a.kinds for k in ("p_high", "top", "lower_high"))
            fig.add_trace(go.Scatter(
                x=[a.date], y=[df.loc[a.date, "high"] * 1.01 if is_high else df.loc[a.date, "low"] * 0.99],
                mode="markers", marker=dict(symbol="triangle-down" if is_high else "triangle-up",
                                            size=10, color=color, line=dict(width=1, color="white")),
                hovertext=f"{a.label} · {a.date:%d %b %Y}", hoverinfo="text", showlegend=False), 1, 1)
    if show_ma:
        fig.add_trace(go.Scatter(x=v.index, y=v["ema20"], name="EMA20",
                                 line=dict(color="#999", width=1, dash="dot")), 1, 1)
        fig.add_trace(go.Scatter(x=v.index, y=v["sma10"], name="SMA10",
                                 line=dict(color="#bbb", width=1, dash="dot")), 1, 1)
    colors = np.where(v["close"] >= v["open"], "rgba(26,163,122,.6)", "rgba(229,72,77,.6)")
    fig.add_trace(go.Bar(x=v.index, y=v["volume"], marker_color=colors, name="Volume", showlegend=False), 2, 1)
    fig.add_trace(go.Scatter(x=v.index, y=v["avg_vol50"], line=dict(color="#888", width=1),
                             name="50d avg vol", showlegend=False), 2, 1)
    fig.add_trace(go.Scatter(x=v.index, y=v["rsi"], line=dict(color="#8a5cd6", width=1.2),
                             name="RSI14", showlegend=False), 3, 1)
    fig.add_hline(y=70, line=dict(color="#aaa", width=0.6, dash="dot"), row=3, col=1)
    fig.add_hline(y=30, line=dict(color="#aaa", width=0.6, dash="dot"), row=3, col=1)
    fig.update_layout(height=720, margin=dict(l=8, r=8, t=10, b=8), xaxis_rangeslider_visible=False,
                      legend=dict(orientation="h", y=-0.06, font=dict(size=10)),
                      hovermode="x unified", dragmode="pan")
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    fig.update_yaxes(title_text="RSI", row=3, col=1, range=[0, 100])
    return fig


def page_chart():
    syms = symbols()
    if not syms:
        st.info("Universe is empty.")
        return
    cur = st.session_state.get("symbol")
    idx = syms.index(cur) if cur in syms else 0
    sym = st.selectbox("Stock", syms, index=idx)
    if sym != cur:
        st.session_state.symbol = sym
        st.query_params["symbol"] = sym
    an = analyze_symbol(db, cfg, sym)
    if an is None:
        st.warning("Not enough price history for this stock yet.")
        return
    s = an.summary()
    uni = db.q("SELECT company, sector, exchange FROM universe WHERE nse_symbol=?", (sym,))
    if len(uni):
        tag = " · data from BSE (not listed on NSE)" if uni["exchange"].iat[0] == "BSE" else ""
        st.caption(f"{uni['company'].iat[0]} · {uni['sector'].iat[0]}{tag}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Close", f"{s['close']:,.2f}", f"{(s['close'] / an.df['close'].iat[-2] - 1) * 100:+.2f}%")
    c2.metric("Primary AVWAP", f"{s['primary_avwap']:,.2f}", f"{s['primary_dist_pct']:+.2f}% from price")
    c3.metric("Major-low AVWAP", f"{s['major_low_avwap']:,.2f}" if s["major_low_avwap"] == s["major_low_avwap"] else "–")
    c4.metric("Volume vs 20d", f"{s['vol_ratio']:.1f}×" if s["vol_ratio"] == s["vol_ratio"] else "–")
    st.caption(f"Primary: **{s['primary_label']}** ({s['primary_date']}) · rule: {s['primary_reason']} · "
               f"RSI {s['rsi']:.0f} · ATR {s['atr']:.2f}")
    if an.signals:
        st.success(" · ".join(f"**{x['name']}**{' 💪' if x['vol_confirmed'] else ''}: {x['detail']}"
                              for x in an.signals))

    keys = [a.key for a in an.anchors]
    labels = {a.key: f"{'★ ' if an.primary and a.key == an.primary.key else ''}{a.label} · {a.date:%d %b %y}"
                     f"{' (manual)' if a.source == 'manual' else ''}" for a in an.anchors}
    with st.expander("AVWAP lines and display", expanded=False):
        visible = st.multiselect("Show AVWAPs", keys, default=keys, format_func=labels.get)
        c1, c2 = st.columns(2)
        months = c1.select_slider("Range", options=[3, 6, 9, 12, 18, 24], value=9,
                                  format_func=lambda m: f"{m} mo")
        show_ma = c2.checkbox("EMA20 / SMA10", value=False)
    st.plotly_chart(build_figure(an, set(visible), show_ma, months), use_container_width=True,
                    config={"displaylogo": False, "scrollZoom": True,
                            "modeBarButtonsToRemove": ["lasso2d", "select2d"]})

    if an.clusters:
        st.markdown("**Clusters**")
        st.dataframe(pd.DataFrame([{
            "Zone": f"{c['low']:.2f} – {c['high']:.2f}", "Where": c["position"],
            "% from price": round(c.get("dist_pct", 0), 2), "Anchors": ", ".join(c["labels"])}
            for c in an.clusters]), hide_index=True, use_container_width=True)

    st.markdown("**Anchors** (automatic in blue/green tones, manual dashed in orange/pink)")
    st.dataframe(pd.DataFrame([{
        "Date": a.key, "Type": a.label, "Source": a.source,
        "AVWAP now": round(float(an.lines[a.key].iat[-1]), 2),
        "% from price": round((s["close"] / float(an.lines[a.key].iat[-1]) - 1) * 100, 2),
        "Primary": "★" if an.primary and a.key == an.primary.key else ""} for a in an.anchors]),
        hide_index=True, use_container_width=True)

    with st.expander("Edit anchors (add · delete · pin)"):
        t1, t2, t3 = st.tabs(["Add", "Delete", "Pin"])
        with t1:
            with st.form("add_anchor"):
                d = st.date_input("Anchor date", value=an.df.index[-20].date(),
                                  min_value=an.df.index[0].date(), max_value=an.df.index[-1].date())
                lbl = st.text_input("Label", "Manual")
                if st.form_submit_button("Add anchor"):
                    write("INSERT INTO manual_anchors (symbol, date, label, action, created_at) "
                            "VALUES (?,?,?,?,datetime('now'))", (sym, d.isoformat(), lbl, "add"))
                    st.rerun()
        with t2:
            k = st.selectbox("Anchor to delete", keys, format_func=labels.get, key="del")
            if st.button("Delete anchor"):
                write("DELETE FROM manual_anchors WHERE symbol=? AND date=? AND action='add'", (sym, k))
                write("INSERT INTO manual_anchors (symbol, date, label, action, created_at) "
                        "VALUES (?,?,?,?,datetime('now'))", (sym, k, None, "delete"))
                st.rerun()
            deleted = db.q("SELECT id, date FROM manual_anchors WHERE symbol=? AND action='delete'", (sym,))
            if len(deleted):
                r = st.selectbox("Restore a deleted automatic anchor", deleted["date"].tolist())
                if st.button("Restore"):
                    write("DELETE FROM manual_anchors WHERE symbol=? AND date=? AND action='delete'", (sym, r))
                    st.rerun()
        with t3:
            pin = db.q("SELECT anchor_date FROM pins WHERE symbol=?", (sym,))
            if len(pin):
                st.write(f"Pinned: **{pin['anchor_date'].iat[0]}**")
                if st.button("Unpin (use automatic rules)"):
                    write("DELETE FROM pins WHERE symbol=?", (sym,))
                    st.rerun()
            k = st.selectbox("Pin as primary", keys, format_func=labels.get, key="pin")
            if st.button("Pin"):
                a = next(x for x in an.anchors if x.key == k)
                write("INSERT OR REPLACE INTO pins VALUES (?,?,?)", (sym, k, a.label))
                st.rerun()


# =============================================================== POSITIONS
def page_positions():
    st.subheader("My positions")
    pos = db.q("SELECT * FROM positions WHERE closed=0")
    rows = []
    for p in pos.to_dict("records"):
        an = analyze_symbol(db, cfg, p["symbol"])
        if an is None or len(an.df) < 2:
            st.warning(f"No price data for {p['symbol']}")
            continue
        ev = evaluate_position({**p, "last_hl_seen": p.get("last_hl_seen")}, an)
        rows.append(ev)
    if rows:
        t = pd.DataFrame(rows)
        tot = float(t["pnl"].sum())
        st.metric("Open P/L", f"₹{tot:,.0f}")
        view = t[["symbol", "close", "profit_pct", "entry_price", "entry_avwap", "dist_entry_pct",
                  "trailing_avwap", "dist_trailing_pct", "t_l1", "t_u1", "t_u2", "trailing_label",
                  "trailing_date", "qty", "id"]]
        st.dataframe(view.rename(columns={
            "symbol": "Symbol", "close": "Close", "profit_pct": "P/L %", "entry_price": "Entry",
            "entry_avwap": "Entry AVWAP", "dist_entry_pct": "% vs entry AVWAP",
            "trailing_avwap": "Trailing AVWAP", "dist_trailing_pct": "% vs trailing",
            "t_l1": "Trail −1σ", "t_u1": "Trail +1σ", "t_u2": "Trail +2σ",
            "trailing_label": "Trailing anchor", "trailing_date": "Trail date", "qty": "Qty"}),
            hide_index=True, use_container_width=True,
            column_config={c: st.column_config.NumberColumn(format="%.2f") for c in
                           ["Close", "Entry", "Entry AVWAP", "Trailing AVWAP", "Trail −1σ", "Trail +1σ", "Trail +2σ"]}
            | {c: st.column_config.NumberColumn(format="%+.2f%%") for c in
               ["P/L %", "% vs entry AVWAP", "% vs trailing"]})
        for ev in rows:
            for a in ev["alerts"]:
                st.warning(f"**{ev['symbol']}**: {a}")
        with st.expander("Change trailing anchor / close a position"):
            pid = st.selectbox("Position", [r["id"] for r in rows],
                               format_func=lambda i: next(f"{r['symbol']} (entry {r['entry_date']})"
                                                          for r in rows if r["id"] == i))
            r = next(x for x in rows if x["id"] == pid)
            an = analyze_symbol(db, cfg, r["symbol"])
            opts = ["auto"] + [a.key for a in an.anchors if a.date >= pd.Timestamp(r["entry_date"])]
            choice = st.selectbox("Trailing anchor", opts,
                                  format_func=lambda k: "Automatic (latest higher low after entry)" if k == "auto"
                                  else next(f"{a.label} · {k}" for a in an.anchors if a.key == k))
            c1, c2 = st.columns(2)
            if c1.button("Save trailing anchor"):
                write("UPDATE positions SET trailing_anchor_date=? WHERE id=?",
                        (None if choice == "auto" else choice, pid))
                st.rerun()
            if c2.button("Close position"):
                write("UPDATE positions SET closed=1 WHERE id=?", (pid,))
                st.rerun()
    else:
        st.info("No open positions.")
    with st.form("add_pos"):
        st.markdown("**Add a holding** (an entry anchor is added automatically)")
        syms = symbols()
        c1, c2 = st.columns(2)
        sym = c1.selectbox("Symbol", syms)
        d = c2.date_input("Entry date", value=date.today())
        c3, c4 = st.columns(2)
        px = c3.number_input("Entry price", min_value=0.0, step=0.05, format="%.2f")
        q = c4.number_input("Quantity", min_value=0.0, step=1.0)
        if st.form_submit_button("Add position") and px > 0:
            write("INSERT INTO positions (symbol, entry_date, entry_price, qty) VALUES (?,?,?,?)",
                    (sym, d.isoformat(), px, q))
            st.rerun()


# =============================================================== STATUS
def page_status():
    st.subheader("Status")
    if CLOUD and st.button("Reload latest data from GitHub"):
        get_db.clear()
        st.rerun()
    runs = db.q("SELECT trade_date, status, message, started_at, finished_at FROM runs ORDER BY id DESC LIMIT 20")
    last = db.last_loaded_day()
    uni = db.q("SELECT exchange, COUNT(*) n FROM universe WHERE active=1 GROUP BY exchange")
    c1, c2 = st.columns(2)
    c1.metric("Last data day", last or "–")
    c2.metric("Universe", int(uni["n"].sum()) if len(uni) else 0,
              f"{int(uni.loc[uni.exchange == 'BSE', 'n'].sum())} BSE-only" if len(uni) else None)
    st.markdown("**Recent runs**")
    st.dataframe(runs, hide_index=True, use_container_width=True)
    st.markdown("**Universe changes**")
    st.dataframe(db.q("SELECT * FROM universe_history ORDER BY date DESC LIMIT 100"),
                 hide_index=True, use_container_width=True)
    bse = db.q("SELECT nse_symbol, company, isin FROM universe WHERE active=1 AND exchange='BSE'")
    if len(bse):
        st.markdown("**Not listed on NSE (BSE bhavcopy used)**")
        st.dataframe(bse, hide_index=True, use_container_width=True)
    st.markdown("**Corporate actions applied**")
    st.dataframe(db.q("SELECT symbol, ex_date, kind, factor, subject FROM corp_actions ORDER BY ex_date DESC LIMIT 100"),
                 hide_index=True, use_container_width=True)


{"Scan": page_scan, "Chart": page_chart, "Positions": page_positions, "Status": page_status}[page]()
