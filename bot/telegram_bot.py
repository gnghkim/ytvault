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
from telegram.request import HTTPXRequest
import health_state
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


# getUpdates 응답 코드 → 헬스체크에 남길 오류 종류
_POLL_ERROR_KINDS = {401: "auth", 404: "auth", 409: "conflict", 429: "rate_limit"}


class _PollTrackingRequest(HTTPXRequest):
    """getUpdates(롱 폴링) 결과를 헬스체크 상태 파일에 남긴다.

    새 메시지가 없어도 텔레그램은 몇 초마다 빈 응답을 돌려주므로, 성공 시각이 계속 갱신되면
    폴링이 정상이다. 시간 초과(TimedOut)는 python-telegram-bot이 오류 처리기로 넘기지 않아
    여기서 직접 잡는다.
    """

    async def do_request(self, *args, **kwargs):
        try:
            code, payload = await super().do_request(*args, **kwargs)
        except Exception as e:
            health_state.bot_poll_error("network", e)
            raise
        if 200 <= code < 300:
            health_state.bot_poll_ok()
        else:
            kind = _POLL_ERROR_KINDS.get(code, "server" if code >= 500 else "http")
            health_state.bot_poll_error(kind, f"HTTP {code}")
        return code, payload


def run_bot():
    global _application, _loop
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        # ApplicationBuilder 기본값과 같은 설정(getUpdates 전용 연결 1개)에 기록만 더한다
        .get_updates_request(_PollTrackingRequest(connection_pool_size=1))
        .build()
    )
    _application = app

    app.add_handler(CommandHandler("start", _handle_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    app.add_handler(CallbackQueryHandler(_handle_resolution_choice, pattern=r"^res:"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop

    logger.info("Telegram bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
