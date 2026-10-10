import logging
import threading
import uvicorn
from log_redaction import configure_safe_logging
from db.models import init_db, requeue_interrupted_jobs
from worker.downloader import run_worker
from bot.telegram_bot import run_bot, send_message_sync
from web.app import app as web_app
from config import WEB_HOST, WEB_PASSWORD, WEB_PORT

configure_safe_logging()
logger = logging.getLogger(__name__)


def start_web():
    uvicorn.run(web_app, host=WEB_HOST, port=WEB_PORT, log_level="warning")


def start_worker():
    run_worker(notify_callback=send_message_sync)


if __name__ == "__main__":
    init_db()
    logger.info("DB initialized.")
    if requeued := requeue_interrupted_jobs():
        logger.info(f"Requeued {requeued} job(s) interrupted by the last shutdown.")

    web_thread = threading.Thread(target=start_web, daemon=True, name="web")
    web_thread.start()
    logger.info(f"Web UI: http://{WEB_HOST}:{WEB_PORT}")
    if not WEB_PASSWORD:
        logger.warning("WEB_PASSWORD is not set: the web UI answers 503 to every request.")

    worker_thread = threading.Thread(target=start_worker, daemon=True, name="worker")
    worker_thread.start()

    # Telegram bot runs in main thread (blocking)
    run_bot()
