import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/anomaly_bot.db")
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "120"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

SOFASCORE_BASE = "https://api.sofascore.com/api/v1"

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
