# FT Anomaly Bot

A lightweight football anomaly detection platform with:

- Anomaly scanner worker (30-85 minute window)
- Telegram notifications
- Modern web dashboard
- SQLite persistence
- Automatic finished-match grading every 30 minutes

Designed for low-resource VPS environments (1 vCPU / 1 GB RAM class).

## Features

### Worker 1: Anomaly Scanner

Runs periodically and analyzes live football matches between minute 30 and 85.

Detects anomalies using two rule groups:

- Condition A (draw matches)
- Condition B (exactly 1-goal difference)

When rules are triggered, it sends formatted Turkish Telegram alerts and stores results in SQLite.

### Worker 2: Finished Match Grading

Runs once at startup and then every 30 minutes. It checks every pending signal
match, archives finished matches, stores the final score, and grades a win bet
on the signal's superior team. Draws count as failed win bets.

### Web Dashboard

- Turkish UI
- System status indicator (active/passive)
- Anomaly table with filtering
- Bulk selection and bulk actions
- Row actions:
  - Bahis Oynandi (bet placed)
  - Gozardi Et (ignored)
  - Takip Et (following)
- Persistent row state in SQLite
- Match-wide actions: every current and future signal inherits the match state
- Clickable `2. sinyal`, `3. sinyal`, etc. labels for match-only filtering
- Manual upcoming-fixture refresh (no scheduled AI analysis or Telegram report)
- Signal-level success/failure grading with a success-rate summary
- Finished-score, superior-team, and result filters in the archive

## Tech Stack

- Python 3.11+
- FastAPI
- APScheduler
- SQLite + aiosqlite
- aiohttp
- curl_cffi (Sofascore access via browser impersonation)
- Vanilla HTML/CSS/JS dashboard

## Project Structure

```text
ft-anomaly-bot/
├── main.py
├── workers.py
├── scraper.py
├── detector.py
├── notifier.py
├── db.py
├── config.py
├── requirements.txt
├── .env.example
├── templates/
│   └── dashboard.html
└── static/
    ├── style.css
    ├── app.js
    └── favicon.svg
```

## Environment Variables

Copy `.env.example` to `.env` and fill values:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DATABASE_PATH=data/anomaly_bot.db
SCAN_INTERVAL_SECONDS=120
FINISHED_SCAN_INTERVAL_MINUTES=30
HOST=0.0.0.0
PORT=8080
```

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Dashboard:

- http://localhost:8080

## systemd

The included `ft-anomaly-bot.service` runs the application from the local
`venv`, restarts it after failures, and starts it automatically at boot:

```bash
sudo install -m 0644 ft-anomaly-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ft-anomaly-bot.service
```

Check it with `systemctl status ft-anomaly-bot.service` and follow logs with
`journalctl -u ft-anomaly-bot.service -f`.

## Scheduler Jobs

- `anomaly_scan`: interval job, every `SCAN_INTERVAL_SECONDS`
- `finished_match_scan`: interval job, every 30 minutes by default

## API Endpoints

- `GET /` dashboard
- `GET /api/status`
- `GET /api/anomalies`
- `POST /api/anomalies/{id}/status`
- `POST /api/anomalies/bulk-status`
- `POST /api/anomalies/delete`
- `GET /api/anomalies/deleted?result=successful|failed|pending|unresolved`
- `POST /api/trigger/upcoming-scan` (manual fixture refresh only)

## Notes

- This app intentionally avoids heavy infrastructure (Redis/RabbitMQ/Postgres).
- SQLite WAL mode is enabled for low overhead and acceptable concurrent behavior.
- `curl_cffi` is used to improve reliability against anti-bot protections on data sources.

## Disclaimer

Sports data source behavior can change over time (rate limits, anti-bot, endpoint changes). Keep scraper logic updated as needed.
