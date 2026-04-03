# Russell 2000 Intraday +1% Incremental Uptrend Range Scanner

**Rebuilt full app package:** reconstructed from the bundled patch chain through **v1.18.0** and checked for internal consistency.

Verification performed in this rebuild:
- Python source compiled successfully across the `app/` package
- SQLite validation-run persistence check passed
- SQLite research-run persistence check passed

Latest app change in this package: **v1.36.1 recent live scan export for the last five sessions at the validated 120/150 checkpoints**.

---

## What this version does

This app answers a narrow intraday question:

> Among the strongest Russell 2000 names so far today, which ones still have a credible structure for a further +1.00% move before the close from a rational tactical entry zone?

It does that in two stages:

1. **Stage 1 — fixed target group**
   - loads a pragmatic Russell 2000 proxy universe
   - computes intraday % gain from regular-session open to the chosen checkpoint
   - ranks the whole universe
   - selects **exactly the top 50 positive movers**

2. **Stage 2 — continuation opportunity evaluation**
   - runs safety / quality filters on that fixed top-50 set
   - calculates explicit, inspectable sub-scores
   - estimates a **dynamic adaptive range**
   - classifies structure as:
     - **A** = incrementally upward-shifting range
     - **B** = static sideways range
     - **C** = unstable non-range behaviour
   - ranks surviving names by a weighted **Continuation Opportunity Score**

The same stage-1 and stage-2 logic is reused in replay validation.

---

## Architecture summary

- **FastAPI** web app
- **Jinja templates + lightweight JS** frontend
- **SQLite** persistence for scans, candidates, validation runs, and cached universe data
- **Alpaca HTTP integration** for snapshots, latest quotes, historical minute bars, daily bars, and optional order submission
- **APScheduler** optional checkpoint scheduler
- **Plotly** charts for candidate detail and validation views
- **Deterministic scoring** instead of opaque ML for v1

---

## Core design decisions

1. **Deterministic, inspectable scoring over ML**  
   Version 1 is deliberately transparent. Every score is a weighted combination of explicit sub-scores and raw metrics.

2. **Checkpoint scans rather than “whatever the latest price is”**  
   Manual and scheduled scans replay the market at a selected checkpoint (30 / 60 / 90 / 120 minutes after the open). This makes live use and historical replay comparable.

3. **Top-50 target group is hard-coded by design**  
   This was kept fixed because the thesis starts from the day’s strongest movers, not from the whole universe equally.

4. **Pragmatic Russell 2000 proxy**  
   The universe is loaded from the iShares IWM holdings file and optionally filtered through Alpaca active/tradable metadata. This is documented as a practical operational proxy, not a licensed official index feed.

5. **Validation is explicit about approximations**  
   Historical fills are not faked. Entry is approximated consistently from the suggested entry zone, and all outputs are presented as replay diagnostics rather than execution-certainty claims.

---

## File tree

```text
r2k_incremental_uptrend_scanner_v1/
├── .env.example
├── README.md
├── render.yaml
├── requirements.txt
├── start.sh
├── data/
│   ├── cache/
│   │   └── .gitkeep
│   └── logs/
│       └── .gitkeep
└── app/
    ├── __init__.py
    ├── config.py
    ├── db.py
    ├── logging_config.py
    ├── main.py
    ├── version.py
    ├── services/
    │   ├── adaptive_range.py
    │   ├── alpaca_client.py
    │   ├── backtest.py
    │   ├── diagnostics.py
    │   ├── market_time.py
    │   ├── scanner.py
    │   ├── scoring.py
    │   ├── stage1.py
    │   ├── trading.py
    │   └── universe.py
    ├── static/
    │   ├── css/
    │   │   └── style.css
    │   └── js/
    │       └── app.js
    └── templates/
        ├── base.html
        ├── candidate_detail.html
        ├── diagnostics.html
        ├── index.html
        ├── scan_detail.html
        ├── settings.html
        └── validation.html
```

---


## Critical Render deployment note

Render now defaults new Python services to **Python 3.14.3** unless you pin a version. This project should be pinned to **Python 3.12.9** for reliable wheel availability during build.

This patch includes both:
- a repo-root `.python-version` file set to `3.12.9`
- `PYTHON_VERSION=3.12.9` in `render.yaml`

If your Render service already exists and was not created from the blueprint, also set `PYTHON_VERSION=3.12.9` manually in the Render Dashboard under **Environment**, then redeploy.

## Setup instructions

### 1) Unzip the package

Unzip the project into a folder.

### 2) Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```bash
pip install -r requirements.txt
```

### 4) Create your environment file

Copy `.env.example` to `.env` and fill in your Alpaca credentials.

Minimum required values:

```env
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_DATA_FEED=iex
TRADING_MODE=scan_only
ENABLE_LIVE_TRADING=false
```

If you have Algo Trader Plus and want full SIP coverage, set:

```env
ALPACA_DATA_FEED=sip
```

### 5) Start the app locally

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://127.0.0.1:8000
```

---

## Render deployment instructions

### 1) Create a new Render Web Service

Use the project root as the deployed repository root.

### 2) Configure a persistent disk

Mount a disk at:

```text
/var/data
```

Recommended size: **5 GB** minimum.

### 3) Set environment variables in Render

At minimum:

```env
APP_ENV=production
DATA_DIR=/var/data
DATABASE_PATH=/var/data/scanner.db
SETTINGS_OVERRIDE_PATH=/var/data/settings_override.json
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_DATA_FEED=iex
TRADING_MODE=scan_only
ENABLE_LIVE_TRADING=false
DEFAULT_SCAN_OFFSET_MINUTES=60
SCHEDULED_SCAN_OFFSETS=30,60,90,120
ENABLE_SCHEDULER=true
```

### 4) Build and start commands

Build:

```bash
pip install -r requirements.txt
```

Start:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### 5) Scheduler behaviour on Render

This build runs APScheduler **inside the web process**.

That means:
- it is fine for a **single-instance** first deployment
- you should avoid multiple web instances if you want to prevent duplicate scheduled scans
- if the service sleeps, restarts, or scales horizontally, scheduled execution can drift or duplicate

For v2, move scheduled jobs into a dedicated worker or external job runner.

---

## Key endpoints

### UI routes

- `/` — live scanner home
- `/scan/{scan_id}` — stored scan detail
- `/scan/{scan_id}/candidate/{symbol}` — candidate detail
- `/validation` — validation runner and stored results
- `/settings` — runtime override settings
- `/diagnostics` — config, universe, logs, health context

### JSON / utility routes

- `/healthz` — health endpoint
- `/status` — admin/status JSON snapshot
- `/api/latest-scan` — latest scan JSON
- `/validation/{id}/summary.json` — validation JSON export
- `/validation/{id}/rows.csv` — validation CSV export

---

## How scoring works

The final **Continuation Opportunity Score** is a weighted sum of explicit component scores.

Default weights:

- target strength: **15%**
- liquidity: **15%**
- volatility capacity: **15%**
- dynamic range: **25%**
- range position: **15%**
- time feasibility: **10%**
- execution quality: **5%**

### 1) Target group strength

Measures whether the name still deserves attention within the top-50 target cohort.

Inputs include:
- intraday % gain from open
- mover rank within the Russell proxy universe
- distance from the #1 mover

### 2) Liquidity / market activity

Measures whether the name is liquid enough to matter operationally.

Inputs include:
- cumulative intraday volume
- 20-day average daily volume
- 20-day average daily dollar volume
- relative volume proxy
- spread penalty
- low-price penalty

### 3) Volatility / continuation capacity

Measures whether the name has enough movement capacity to plausibly travel another +1.00%.

Inputs include:
- realized intraday volatility
- ATR-like % measure
- session range %
- recent bar range behaviour
- late-session penalty

### 4) Dynamic range establishment

This is the most thesis-specific piece.

The app builds a rolling adaptive band using recent minute bars and scores whether the band is:

- **contained enough** to behave like a range
- **upward-sloping enough** to be progressive rather than flat
- **stable enough** to avoid violent boundary step-changes
- **participatory enough** to show repeated interaction inside the band

It explicitly classifies the current structure into:

- **A — incrementally upward-shifting range**
- **B — static sideways range**
- **C — unstable non-range behaviour**

Only **A** should score highly.

### 5) Position within the shifting range

The app prefers names where price is still in the lower-to-mid region of the current adaptive band.

That avoids rewarding pure strength alone and tries to reduce top-tick chasing.

### 6) Time feasibility

The app penalizes setups that do not have enough remaining session time to plausibly achieve +1.00%.

Inputs include:
- minutes until close at checkpoint
- recent pace of movement
- required pace to achieve target before close

### 7) Execution quality

The app penalizes poor practical tradeability.

Inputs include:
- spread proxy
- relative activity
- “chase” penalty if price is already too high in the band
- structural penalty when classification is B or C

---

## Adaptive shifting-range logic

The adaptive range is **not** a static box.

For recent minute bars, the app computes:

- rolling low band
- rolling high band
- rolling midline
- current normalized band width
- slope of the midline and both boundaries
- containment ratio of closes inside the band
- band step-change penalty
- higher-low and higher-high ratios
- reversal count

That produces a **dynamic range score** plus the A/B/C classification.

### Suggested entry zone

The suggested entry zone is intentionally **not** just “current price”.

It is estimated from the lower portion of the current adaptive band:

- `entry_low = band_low + 5% of width`
- `entry_high = band_low + 33% of width`

The UI then shows:
- current price
- entry zone
- distance from current price to entry zone
- +1.00% target from entry proxy
- optional +2.00% stretch target
- indicative stop below the band

---

## Validation logic

Validation replays the same process historically:

1. choose a trading date and checkpoint
2. compute the day’s top 50 movers at that checkpoint
3. apply stage-2 filtering and scoring
4. model an entry proxy from the suggested entry zone
5. test whether +1.00% from that proxy was reached before the close

### What the app reports

Minimum outputs included:

- historical scan replay by date
- top-50 stage-1 replay at checkpoint
- stage-2 replay over that cohort
- hit rate by score bucket
- hit rate by mover-rank bucket
- precision@5 / @10 / @20
- average max favourable excursion
- average max adverse excursion
- median minutes to target
- sample false positives
- downloadable JSON and CSV

### Entry approximation used in replay

To avoid pretending to exact fills:

- if checkpoint price is already inside the entry zone, the proxy entry is checkpoint price
- otherwise, the replay uses the midpoint of the suggested entry zone as the proxy entry

This is an approximation by design and is disclosed rather than hidden.

---

## Settings and controls

The Settings page can persist non-secret runtime overrides to:

```text
data/settings_override.json
```

Supported runtime overrides include:
- trading mode
- notional amount
- price / liquidity thresholds
- target percentages
- default checkpoint offset
- scheduled offsets
- component weights

Secrets remain environment-variable only.

---

## Trading safety

The app defaults to:

```text
TRADING_MODE=scan_only
ENABLE_LIVE_TRADING=false
```

Optional manual order submission is included for paper/live modes, but:
- it is not the main product behaviour
- it requires deliberate mode selection
- live trading is explicitly blocked unless `ENABLE_LIVE_TRADING=true`

---

## Assumptions

1. **Universe** is a pragmatic Russell 2000 proxy from IWM holdings, not an official licensed membership feed.
2. **Checkpoint scans** replay the chosen minute-after-open timestamp rather than continuously rescoring on every later minute.
3. **Spread proxy** uses latest quote in live mode and a bar-range-based proxy in historical replay.
4. **Validation fills** are approximated consistently rather than pretending exact historical execution quality.
5. **Minute bars** are the main structural input for this build; there is no tick-level execution model in v1.

---

## Known limitations

1. **Universe is a proxy, not a licensed official Russell feed.**
2. **Render scheduler is single-process only.** Multi-instance deployments can duplicate jobs.
3. **Historical validation can be slow** over large date ranges because it fetches many minute bars.
4. **Spread and slippage are still approximations** rather than full order-book execution modelling.
5. **No asynchronous job queue yet.** Validation runs happen inside the web request lifecycle.
6. **No trained ML layer yet.** This is intentional for transparency, but it limits nonlinear pattern detection.
7. **Halts and abnormal behaviour are only partially detectable** from missing bars, volume, and spread proxies.

---

## Suggested v2 improvements

1. Move scheduler and replay jobs into a dedicated worker.
2. Cache historical bar slices more aggressively by day and symbol chunk.
3. Add richer halt / abnormal-trading detection.
4. Add event overlays (news / halts / corporate actions) for candidate context.
5. Add model calibration layer on top of the deterministic score, while preserving feature transparency.
6. Add smarter entry replay logic that requires actual post-scan retracement into the zone.
7. Add cross-sectional normalization by checkpoint regime and sector / industry context.
8. Add structured score diagnostics comparing winners vs false positives across buckets.

---

## Local quick-start

```bash
cp .env.example .env
# fill in Alpaca credentials
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

## Health checks before you trust outputs

Before using the scanner seriously, confirm:

- `/healthz` returns `ok: true`
- `/status` shows valid data API access
- the universe count looks reasonable
- the latest scan contains a full top-50 target group
- validation runs complete successfully on a small date range first

