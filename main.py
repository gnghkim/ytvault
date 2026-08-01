import logging
import threading
import uvicorn
from db.models import init_db
from worker.downloader import run_worker
from bot.telegram_bot import run_bot, send_message_sync
from web.app import app as web_app
from config import WEB_HOST, WEB_PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def start_web():
    uvicorn.run(web_app, host=WEB_HOST, port=WEB_PORT, log_level="warning")


def start_worker():
    run_worker(notify_callback=send_message_sync)


if __name__ == "__main__":
    init_db()
    logger.info("DB initialized.")

    web_thread = threading.Thread(target=start_web, daemon=True, name="web")
    web_thread.start()
    logger.info(f"Web UI: http://{WEB_HOST}:{WEB_PORT}")

    worker_thread = threading.Thread(target=start_worker, daemon=True, name="worker")
    worker_thread.start()
    logger.info("Worker started.")

    # Telegram bot runs in main thread (blocking)
    run_bot()
