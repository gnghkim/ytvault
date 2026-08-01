import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_CHAT_IDS = [
    int(cid.strip())
    for cid in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if cid.strip()
]

GDRIVE_FOLDER_ID = os.getenv("GDRIVE_FOLDER_ID", "")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
DB_PATH = os.getenv("DB_PATH", "./ytvault.db")

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
