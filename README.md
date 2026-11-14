# AVWAP Scanner (BSE 500)

A personal end-of-day scanner that anchors VWAPs on the important pivots of every BSE 500
stock, finds AVWAP clusters, flags daily setups, tracks your positions with AVWAP trailing
levels, and posts a summary to your Telegram channel. Includes a phone-friendly web dashboard.

It reports levels and conditions only. It does not place orders or make recommendations.

## What's inside

| Part | Where |
|---|---|
| NSE/BSE bhavcopy download, holidays, corporate actions, results dates | `avwap/data/` |
| AVWAP + σ bands, indicators, anchors, primary choice, clusters, signals, positions | `avwap/engine/` |
| Full-universe scan, stored in SQLite | `avwap/scan.py` |
| Evening job with retries and error alerts, scheduler | `avwap/runner.py` |
| Telegram summary | `avwap/alerts/telegram.py` |
| Dashboard (Streamlit + Plotly) | `dashboard/app.py` |
| All thresholds | `settings.yaml` |

## Setup

Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
python -m avwap init
```

### 1. Telegram

1. In Telegram, talk to **@BotFather**, send `/newbot`, and copy the token.
2. Add the bot to your channel as an **admin** (it needs "Post messages").
3. Put the token and the channel id in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=@your_channel_username     # or the numeric id, e.g. -1001234567890
   ```
4. `python -m avwap telegram-test` should post a test message.

`.env` is git-ignored. Never commit the token.

### 2. BSE 500 universe

```bash
python3 -m avwap universe
```

This downloads the current BSE 500 list from bseindia.com (BSE's own website API, the same data
behind https://www.bseindia.com/sensex/code/17), saves it to `data/bse500.csv` and imports it.
It prints how many stocks it found; expect about 500. A stock that closed exactly unchanged that
day can be missed, so if the count is a little short, run it again on another day.

If BSE's site is down, you can import a list you downloaded yourself instead:
`python3 -m avwap universe ~/Downloads/<file>` (CSV/Excel with a scrip code or ISIN column; title
rows are skipped).

Scrip codes are mapped to ISINs (from BSE's securities list, or the latest BSE bhavcopy), and ISINs
to NSE symbols using NSE's `EQUITY_L.csv`. Stocks not listed on NSE are logged, shown as
`BSE-<scrip code>`, priced from the BSE bhavcopy and marked "BSE" in the dashboard and alerts.
The scheduler refreshes the list on the 1st of every month and tells you on Telegram; additions and
removals are kept in `universe_history` (visible on the Status page).

### 3. Backfill history

```bash
python -m avwap backfill --years 3
```

Downloads every NSE bhavcopy (EQ and BE series) for the period, plus corporate actions (splits,
bonuses, consolidations) and quarterly-results dates. Weekdays without a file are recorded as holidays.
This takes a while (one file per day, ~750 files) and can be re-run safely: loaded days are skipped.

### 4. Run it every evening

```bash
python -m avwap schedule
```

Each weekday at 19:00 IST it: checks the NSE holiday calendar (does nothing on holidays) →
downloads the bhavcopy, retrying every 30 minutes until 22:00 → refreshes corporate actions and
results dates → back-adjusts prices → scans every stock → sends the Telegram summary → backs up
the database to `data/backups/`. If the bhavcopy never appears or anything fails, you get an
error message on Telegram and **no scan is run on incomplete data**.

Keep it running with `systemd`, `tmux`, or Windows Task Scheduler, or use cron instead:

```cron
# 19:00 IST weekdays (adjust if your server clock is UTC: 13:30 UTC)
0 19 * * 1-5  cd /path/to/Stock-Trading && .venv/bin/python -m avwap daily >> logs/daily.log 2>&1
```

Manual runs: `python -m avwap daily --date 2026-11-13 --no-wait` or `python -m avwap scan --send`.

### 5. Dashboard

```bash
streamlit run dashboard/app.py          # http://localhost:8501
```

- **Scan**: every stock, sortable; filter by signal, sector, % from primary AVWAP, near-cluster,
  above/below primary. Tap a row to open its chart.
- **Chart**: candles, volume, RSI; every AVWAP (auto anchors solid, manual anchors dashed orange/pink),
  primary drawn thick with ±1σ/±2σ bands, anchor markers, cluster zones shaded. Toggle each AVWAP;
  add, delete, restore or pin anchors. Manual edits survive the daily re-scan.
- **Positions**: add holdings (an entry anchor is added automatically), see profit, entry AVWAP,
  trailing AVWAP and its σ bands, change the trailing anchor, close a position.
- **Status**: recent runs, universe changes, BSE-only stocks, corporate actions applied.

To open it on your iPhone, run it on a VPS or on your PC and reach it over your network
(for example with Tailscale). Set `DASHBOARD_URL` in `.env` so Telegram chart links point there.
Streamlit has no login: don't expose it to the open internet without a VPN or a reverse proxy
with authentication.

Positions can also be managed from the command line:
`python -m avwap position add TECHM 2026-10-01 1520.5 50`, `position list`, `position close 3`.

### Try it without market data

```bash
python -m avwap --settings settings.demo.yaml demo
AVWAP_SETTINGS=settings.demo.yaml streamlit run dashboard/app.py
```

This loads 40 synthetic stocks (one with a 1:5 split, one BSE-only, two positions) into a separate
`data/demo.sqlite`.

## Hosting for free (GitHub Actions + Streamlit Community Cloud)

Run everything without keeping your computer on:

- **GitHub Actions** (`.github/workflows/daily-scan.yml`) runs the scan on weekdays every 30 minutes
  from 19:00 to 22:00 IST until the day's bhavcopy is available, then sends the Telegram summary.
  Later runs that day do nothing. If the file never appears you get one error alert at 22:00. If NSE
  blocks GitHub's servers, the alert says so.
- **Streamlit Community Cloud** serves the dashboard at a public `https://…streamlit.app` URL,
  behind a password.
- Data lives as **encrypted** files on a GitHub release called `data` (see `avwap/cloud.py`).
  They are encrypted with `APP_PASSWORD`, so they stay private even though the repository is public.
  Positions, manual anchors and pins you change on the hosted dashboard are saved there too, and
  the next scan uses them.

What goes where:

| Where | Name | Value |
|---|---|---|
| GitHub → Settings → Secrets and variables → Actions → **Secrets** | `TELEGRAM_BOT_TOKEN` | your bot token |
| | `TELEGRAM_CHAT_ID` | your channel id |
| | `APP_PASSWORD` | a password you choose (dashboard login + data encryption) |
| GitHub → same page → **Variables** | `DASHBOARD_URL` | your `https://…streamlit.app` address |
| Streamlit app → Settings → **Secrets** | `APP_PASSWORD`, `GITHUB_REPO`, `GITHUB_TOKEN` | same password; `ss-sumansaurav/Stock-Trading`; a fine-grained token with *Contents: read and write* on this repository |
| Your computer's `.env` (only to upload existing data) | `APP_PASSWORD`, `GITHUB_REPO`, `GITHUB_TOKEN` | same as Streamlit |

Seed the hosted data once: either run `python3 -m avwap cloud-upload` on the computer that already
has the backfilled database, or open **Actions → Daily AVWAP scan → Run workflow** and choose
`backfill`. The backfill only works if NSE doesn't block GitHub's servers.

Keep `APP_PASSWORD` the same everywhere. Changing it makes the stored data unreadable.
NSE and BSE sometimes block cloud servers (GitHub's runners are in the US). If the daily alerts
report that NSE refused the download, run the daily job on your own computer (step 4 above) and
use `cloud-upload` to keep the hosted dashboard up to date.

## How the calculations work

**AVWAP** (matches TradingView Anchored VWAP, source hlc3): TP = (H+L+C)/3,
AVWAP(t) = Σ TP·V / Σ V from the anchor day (inclusive) to t.
**Bands**: σ(t) = √(Σ V·(TP − AVWAP(t))² / Σ V), shown at ±1σ and ±2σ.

**Indicators**: ATR(14) and RSI(14) with Wilder smoothing, EMA20, SMA10, 20/50-day average volume
(of the prior days, so today's spike is compared with normal volume), 52-week high/low.

**Anchors** (last 252 trading days, thresholds in `settings.yaml`):

| Type | Rule |
|---|---|
| P-high / P-low | Highest high / lowest low with 5 bars each side, and a swing ≥ 2×ATR(14) from the previous pivot (pivots alternate high/low) |
| Major low / Top | Lowest low / highest high of the 12-month window |
| Higher low | Swing low above the previous swing low, after which price trades above the previous swing high |
| Lower high | Mirror image, used for downtrends |
| Volume anchor | Volume ≥ 3× 50-day average |
| Breakout | Volume anchor and close above the prior 20-day high |
| Gap | Open beyond the previous day's range by ≥ 1×ATR(14) |
| Results | Quarterly-results board meeting date from NSE (or add manually) |

Anchors on the same day are merged (e.g. "Top + Gap + Volume anchor"). To keep charts readable
the scanner keeps the major low/top, the latest two breakouts/higher lows/lower highs, and the most
recent others up to `max_auto_anchors`.

**Primary AVWAP**: pinned anchor → most recent breakout in the last 60 days → most recent higher low
that has not been undercut (uptrend) or lower high that has not been exceeded (downtrend) →
12-month major low. The major-low AVWAP is always shown as the long-term line.

**Clusters**: AVWAPs within 1.5% of each other (measured from the lowest member). Above price =
overhead supply, below = support.

**Signals**: cluster reclaim, primary reclaim, primary loss, pullback hold (low within 0.5% of the
primary, close above), all anchors below (close above every AVWAP for the first time in 20 days),
extension (close above +2σ), supply test (close within 2% below the 12-month top or an overhead
cluster). Each is flagged 💪 when volume ≥ 1.5× the 20-day average. Extension and supply test fire on
the day the condition starts (`only_new_events`), so they don't repeat every evening.

**Positions**: entry AVWAP from the entry date; trailing AVWAP from the most recent higher low after
entry (or your choice). Alerts when the close crosses below either, when price reaches +2σ of the
trailing AVWAP, and when a new higher low forms after entry.

**Corporate actions**: splits, bonuses and consolidations are parsed from NSE's corporate-action
feed. Prices before the ex-date are divided by the factor and volumes multiplied. Raw data
(`prices_raw`) is never modified; adjusted data lives in `prices_adj`. Special dividends are not
adjusted.

## Tests

```bash
pytest
```

Covers the AVWAP and σ formulas against a direct implementation, split adjustment (no jump on the
ex-date), anchor detection, primary selection and pins, manual anchors, clusters, signals, the
Telegram summary layout, a missing-bhavcopy run (error alert, no scan) and holidays.
A synthetic 500-stock, 3-year scan runs in about 20 seconds.

## Acceptance checks still to do with real data

- Compare AVWAPs for Optiemus Infracom, Tech Mahindra, City Union Bank and two others with
  TradingView (hlc3) on 3+ anchors each (target within 0.5%).
- Eyeball the automatic anchors on those charts.
- Run unattended for 10 trading days including an NSE holiday.

## Data access notes

NSE blocks requests without browser headers, and its `/api/` endpoints (holidays, corporate actions,
board meetings) need cookies from the home page; `NSESession` handles both and retries. NSE also
rate-limits and occasionally changes formats (it moved to the "UDiFF" bhavcopy format on 8 July 2024;
both formats are supported). Some cloud/VPS IP ranges are blocked by NSE; if downloads fail on a VPS,
run the scanner on your own PC or use an Indian VPS. If the holiday API is unreachable, add
holidays to `data.extra_holidays` in `settings.yaml`.
