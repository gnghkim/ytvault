import re
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from db.models import create_job, get_job_by_url, reset_job_to_pending, STATUS_DONE, STATUS_FAILED
from uploader.gdrive import check_file_exists
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS

logger = logging.getLogger(__name__)

YOUTUBE_URL_RE = re.compile(
    r"(https?://(?:www\.)?(?:"
    r"youtube\.com/watch\?[^\s]*v=[\w-]+"  # watch?v=
    r"|youtube\.com/live/[\w-]+"           # live/
    r"|youtube\.com/shorts/[\w-]+"         # shorts/
    r"|youtu\.be/[\w-]+"                   # youtu.be/
    r")[^\s]*)"
)

_application = None
_loop = None


def _is_allowed(chat_id: int) -> bool:
    if not TELEGRAM_ALLOWED_CHAT_IDS:
        return True
    return chat_id in TELEGRAM_ALLOWED_CHAT_IDS


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    if not _is_allowed(chat_id):
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    urls = YOUTUBE_URL_RE.findall(update.message.text)
    if not urls:
        return

    for url in urls:
        reply = await _resolve_url(url, chat_id)
        await update.message.reply_text(reply)


async def _resolve_url(url: str, chat_id: int) -> str:
    existing = get_job_by_url(url)

    if existing is None:
        job_id = create_job(url, chat_id)
        return f"📥 다운로드 대기 중 (#{job_id})\n{url}"

    if existing["status"] == STATUS_DONE:
        gdrive_link = existing.get("gdrive_link", "")
        file_ok = bool(gdrive_link) and check_file_exists(gdrive_link)

        if file_ok:
            msg = f"✅ 이미 저장된 영상입니다 (#{existing['id']})\n"
            if existing.get("title"):
                msg += f"📌 {existing['title']}\n"
            msg += f"📁 Drive: {gdrive_link}"
            if existing.get("notion_url"):
                msg += f"\n📝 Notion: {existing['notion_url']}"
            return msg

        # Drive 파일이 없으면 재처리
        reset_job_to_pending(existing["id"])
        return f"🔄 Drive 파일이 없어 재다운로드합니다 (#{existing['id']})\n{url}"

    if existing["status"] == STATUS_FAILED:
        reset_job_to_pending(existing["id"])
        return f"🔄 이전에 실패한 작업을 재시도합니다 (#{existing['id']})\n{url}"

    # pending / downloading / uploading / notifying — 이미 진행 중
    return f"⏳ 이미 처리 중입니다 (#{existing['id']}, 상태: {existing['status']})\n{url}"


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "YouTube 링크를 보내면 다운로드 후 Google Drive에 저장합니다."
    )


def send_message_sync(chat_id: int, text: str):
    """Notify user from worker thread (thread-safe)."""
    if _application is None or _loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        _application.bot.send_message(chat_id=chat_id, text=text),
        _loop,
    )


def run_bot():
    global _application, _loop
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    _application = app

    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    logger.info("Telegram bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
