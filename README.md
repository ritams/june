## June Dashboard

June is a small FastAPI app that serves two client-facing dashboards plus an optional combined overview:

- `Liquidity`
- `Business Cycle`

The backend pulls live macro data from `FRED`, `yfinance`, and `Perplexity sonar-pro` when configured, with an official `ISM` fallback path if Perplexity is unavailable. It can also:

- send Telegram alerts
- send a daily Telegram card
- append daily snapshots to Google Sheets when credentials are configured
- build a cached historical playbook for both dashboard pages

By default, external market and macro pulls are cached for `15 minutes`.

## What Is In Scope

- Two client-facing dashboard pages at `/liquidity` and `/business-cycle`
- One optional combined overview at `/dashboard`
- Top-level `RISK ON` / `RISK OFF` signal
- A middle `SELECTIVE` state when liquidity and cycle are mixed
- Historical playbook sections on both pages with backtested forward returns and allocation guidance
- Automatic alert checks every `15 minutes` while the app is running
- Automatic daily card at the configured time
- UK time support via `Europe/London`

## macOS Setup

These instructions are written for running the app on a Mac locally or on a Mac mini.

### 1. Install prerequisites

You need:

- `uv`
- Python available through `uv`

Check `uv`:

```bash
which uv
uv --version
```

If `uv` is missing, install it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then restart your shell or run:

```bash
source ~/.zshrc
```

### 2. Create `.env`

Copy the example file:

```bash
cp .env.example .env
```

Then open `.env` and fill it in.

Minimum required key:

- `FRED_API_KEY`

For this deployment, use a block like this in `.env`:

```env
FRED_API_KEY=your_fred_key
PERPLEXITY_API_KEY=
PERPLEXITY_MODEL=sonar-pro
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
GOOGLE_SHEET_ID=
GOOGLE_SHEET_TAB=Macro Dashboard
GOOGLE_SERVICE_ACCOUNT_FILE=
APP_TIMEZONE=Europe/London
DISPLAY_TIMEZONE=Europe/London
DAILY_CARD_TIME=07:45
ENABLE_SCHEDULER=true
REFRESH_INTERVAL_MINUTES=15
CACHE_TTL_SECONDS=900
HOST=127.0.0.1
PORT=8000
GLOBAL_M2_PROXY_SERIES_ID=
```

Notes:

- `APP_TIMEZONE` controls scheduler timing.
- `DISPLAY_TIMEZONE` controls visible timestamps and the daily card date.
- `Europe/London` is the correct choice for UK local time because it handles GMT and BST automatically.
- `PERPLEXITY_API_KEY` enables the full-build data path for `ISM PMI` and `South Korean Exports`.
- Without `PERPLEXITY_API_KEY`, the app falls back to the official ISM page and FRED series for Korean exports.
- If `GOOGLE_SHEET_ID` and `GOOGLE_SERVICE_ACCOUNT_FILE` are empty, the app still runs and Google Sheets stays disabled.

### 3. Run locally

Start the app:

```bash
uv run python main.py
```

Open:

- `http://127.0.0.1:8000/` redirects to `http://127.0.0.1:8000/liquidity`
- `http://127.0.0.1:8000/liquidity`
- `http://127.0.0.1:8000/business-cycle`
- `http://127.0.0.1:8000/dashboard`

Health check:

```bash
curl http://127.0.0.1:8000/api/health
```

Expected shape:

```json
{
  "ok": true,
  "telegram_enabled": true,
  "google_sheets_enabled": false,
  "perplexity_enabled": false,
  "scheduler_enabled": true,
  "backtest_cache_available": true,
  "backtest_last_calculated": "2026-04-18",
  "backtest_cache_stale": false
}
```

## Historical Playbook

Each dashboard page includes a cached "Historical Playbook" section:

- `Liquidity`: `RISK ON`, `M2 Acceleration`, `Dollar Weakness`
- `Business Cycle`: `RISK OFF`, `Yield Curve Uninversion`, `Credit Stress`, `Macro Summer Entry`

The cache is written to `runtime/backtest_results.json`.

- It refreshes weekly on Sunday when the scheduler is enabled.
- It is also warmed in the background on startup if missing or stale.
- Page loads read the cache and do not trigger recalculation themselves.

## Automatic Restart On macOS

Use `launchd`. This gives you:

- auto start at login
- auto restart if the process crashes
- log files on disk

This repo already includes:

- [scripts/run-dashboard.sh](scripts/run-dashboard.sh)
- [deploy/com.june.dashboard.plist](deploy/com.june.dashboard.plist)

### Important before loading the LaunchAgent

The checked-in plist uses absolute paths for this repo:

- `/Users/ritam/workspace/services/daniel/june/scripts/run-dashboard.sh`
- `/Users/ritam/workspace/services/daniel/june/runtime/...`

If you move this repo to another path or another Mac user, update these paths first:

- [deploy/com.june.dashboard.plist](deploy/com.june.dashboard.plist)
- [scripts/run-dashboard.sh](scripts/run-dashboard.sh)

Also check the `uv` path in the script:

- `/Users/ritam/.local/bin/uv`

If `which uv` returns a different path, update `UV_BIN` in [scripts/run-dashboard.sh](scripts/run-dashboard.sh).

### Load the service

Run this once:

```bash
mkdir -p ~/Library/LaunchAgents
cp /Users/ritam/workspace/services/daniel/june/deploy/com.june.dashboard.plist ~/Library/LaunchAgents/com.june.dashboard.plist
launchctl unload ~/Library/LaunchAgents/com.june.dashboard.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.june.dashboard.plist
launchctl kickstart -k gui/$(id -u)/com.june.dashboard
```

If you already started the app in a terminal, stop that first. Otherwise you may get a port conflict on `8000`.

### Check status

```bash
launchctl list | rg com.june.dashboard
curl http://127.0.0.1:8000/api/health
```

### View logs

```bash
tail -f /Users/ritam/workspace/services/daniel/june/runtime/launchd.stdout.log
tail -f /Users/ritam/workspace/services/daniel/june/runtime/launchd.stderr.log
```

### Restart manually

```bash
launchctl kickstart -k gui/$(id -u)/com.june.dashboard
```

### Stop the service

```bash
launchctl unload ~/Library/LaunchAgents/com.june.dashboard.plist
```

### Remove the service completely

```bash
launchctl unload ~/Library/LaunchAgents/com.june.dashboard.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.june.dashboard.plist
```

## Alert Behavior

Automatic alert checks run every `15 minutes` when the scheduler is enabled.

Current automatic Telegram triggers:

- `Yield curve` crossing above `0`
- `Credit spreads` crossing above `500 bps`
- `DXY` crossing above `105`
- `ISM PMI` crossing `50` in either direction
- `Global M2 Proxy` dropping below `-3% MoM`

Important:

- Alerts trigger on a crossing event, not on every refresh.
- The app must be running for the scheduler to work.
- Perplexity-backed release reads are optional, but the fallback data path is less aligned with the original build brief.

## Google Sheets Setup

Google Sheets is optional.

To enable it:

1. Create a Google service account.
2. Download the JSON credentials file to the Mac.
3. Share the target Google Sheet with the service account email.
4. Set these in `.env`:

- `GOOGLE_SHEET_ID`
- `GOOGLE_SERVICE_ACCOUNT_FILE`

Example:

```env
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_SERVICE_ACCOUNT_FILE=/Users/yourname/path/to/service-account.json
```

If those variables are missing, the dashboard still runs and Sheets logging stays off.

## Public URL

For a quick public test URL on macOS, use Cloudflare Quick Tunnel after the app is running:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

That is fine for testing. For a stable public URL, use a proper Cloudflare Tunnel with your domain.

## Useful Commands

Run tests:

```bash
uv run pytest -q
```

Fetch a live liquidity snapshot:

```bash
curl http://127.0.0.1:8000/api/snapshot
```

Fetch the liquidity dashboard payload, including its playbook cards:

```bash
curl http://127.0.0.1:8000/api/dashboard/liquidity
```

Force refresh the cached data:

```bash
curl -X POST http://127.0.0.1:8000/api/actions/refresh
```

Send the current daily card immediately:

```bash
curl -X POST http://127.0.0.1:8000/api/actions/send-daily-card
```

Recalculate the historical playbook cache manually:

```bash
curl -X POST http://127.0.0.1:8000/api/actions/recalculate-playbook
```
