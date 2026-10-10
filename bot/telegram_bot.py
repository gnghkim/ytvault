import re
import logging
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from db.models import (
    choose_resolution,
    create_job,
    get_job_by_url,
    reset_job_to_pending,
    STATUS_AWAITING_CHOICE,
    STATUS_DONE,
    STATUS_FAILED,
)
from worker.downloader import resolution_label
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

    if existing["status"] == STATUS_AWAITING_CHOICE:
        return f"🎬 해상도 선택을 기다리는 중입니다 (#{existing['id']}). 앞서 보낸 메시지의 버튼에서 골라 주세요.\n{url}"

    # pending / downloading / uploading / notifying — 이미 진행 중
    return f"⏳ 이미 처리 중입니다 (#{existing['id']}, 상태: {existing['status']})\n{url}"


async def _handle_resolution_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """해상도 버튼(callback_data = "res:<job_id>:<height>") 처리."""
    query = update.callback_query
    if not update.effective_chat or not _is_allowed(update.effective_chat.id):
        await query.answer("접근 권한이 없습니다.", show_alert=True)
        return
    try:
        _, job_id, height = query.data.split(":")
        job_id, height = int(job_id), int(height)
    except ValueError:
        await query.answer()
        return

    if choose_resolution(job_id, height):
        label = resolution_label(height)
        logger.info(f"[job {job_id}] resolution chosen: {label}")
        await query.answer(f"{label} 선택")
        await query.edit_message_text(f"{query.message.text}\n\n✅ {label}로 저장합니다. (#{job_id})")
    else:
        await query.answer("이미 해상도가 정해진 작업입니다.")
        await query.edit_message_reply_markup(reply_markup=None)


async def _handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "YouTube 링크를 보내면 다운로드 후 Google Drive에 저장합니다."
    )


def send_message_sync(chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None):
    """Notify user from worker thread (thread-safe).

    buttons: (버튼 글자, callback_data) 목록. 주면 한 줄에 두 개씩 인라인 버튼을 붙인다.
    """
    if _application is None or _loop is None:
        return
    markup = None
    if buttons:
        keys = [InlineKeyboardButton(label, callback_data=data) for label, data in buttons]
        markup = InlineKeyboardMarkup([keys[i:i + 2] for i in range(0, len(keys), 2)])
    asyncio.run_coroutine_threadsafe(
        _application.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup),
        _loop,
    )


def run_bot():
    global _application, _loop
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    _application = app

    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    app.add_handler(CallbackQueryHandler(_handle_resolution_choice, pattern=r"^res:"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    logger.info("Telegram bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
