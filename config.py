import os
from dotenv import load_dotenv

# 실행 위치(cwd)와 무관하게 프로젝트 폴더 기준으로 경로를 해석한다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))


def project_path(path: str) -> str:
    return os.path.join(BASE_DIR, os.path.expanduser(path))


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

DOWNLOAD_DIR = project_path(os.getenv("DOWNLOAD_DIR", "downloads"))
DB_PATH = project_path(os.getenv("DB_PATH", "ytvault.db"))
# 워커·봇이 살아 있다는 기록. ytvault가 쓰고 healthcheck.py가 읽는다
HEALTH_STATE_PATH = project_path(os.getenv("HEALTH_STATE_PATH", "health_state.json"))

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
# HTTP Basic credentials for the web UI. With no password the UI refuses every
# request instead of serving the job list to anyone on the network.
WEB_USERNAME = os.getenv("WEB_USERNAME", "ytvault")
WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
