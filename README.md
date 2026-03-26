# FT Anomaly Bot

A lightweight football anomaly detection platform with:

- Live anomaly scanner worker (20-80 minute window)
- Upcoming matches analysis worker (Gemini)
- Telegram notifications
- Modern web dashboard
- SQLite persistence

Designed for low-resource VPS environments (1 vCPU / 1 GB RAM class).

## Features

### Worker 1: Live Match Anomaly Scanner

Runs periodically and analyzes live football matches between minute 20 and 80.

Detects anomalies using two rule groups:

- Condition A (draw matches)
- Condition B (exactly 1-goal difference)

When rules are triggered, it sends formatted Turkish Telegram alerts and stores results in SQLite.

### Worker 2: Upcoming Match Analysis

Runs at:

- 07:00 Europe/Istanbul
- 19:00 Europe/Istanbul

Flow:

1. Scrapes today's scheduled football matches
2. Sends a structured prompt to Gemini Flash model
3. Saves analysis to SQLite
4. Sends analysis to Telegram

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
GEMINI_API_KEY=
DATABASE_PATH=data/anomaly_bot.db
SCAN_INTERVAL_SECONDS=120
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

## Scheduler Jobs

- `live_scan`: interval job, every `SCAN_INTERVAL_SECONDS`
- `upcoming_morning`: daily at 07:00 Europe/Istanbul
- `upcoming_evening`: daily at 19:00 Europe/Istanbul

## API Endpoints

- `GET /` dashboard
- `GET /api/status`
- `GET /api/anomalies`
- `POST /api/anomalies/{id}/status`
- `POST /api/anomalies/bulk-status`
- `POST /api/anomalies/delete`
- `GET /api/analyses`
- `POST /api/analyses/delete`

## Notes

- This app intentionally avoids heavy infrastructure (Redis/RabbitMQ/Postgres).
- SQLite WAL mode is enabled for low overhead and acceptable concurrent behavior.
- `curl_cffi` is used to improve reliability against anti-bot protections on data sources.

## Disclaimer

Sports data source behavior can change over time (rate limits, anti-bot, endpoint changes). Keep scraper logic updated as needed.
