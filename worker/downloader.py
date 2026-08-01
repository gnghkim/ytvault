import glob
import os
import shutil
import time
import logging
import threading
from datetime import datetime
import yt_dlp
from db.models import (
    get_pending_jobs,
    update_job,
    STATUS_DOWNLOADING,
    STATUS_UPLOADING,
    STATUS_NOTIFYING,
    STATUS_DONE,
    STATUS_FAILED,
)
from uploader.gdrive import upload_video_package
from notifier.notion import save_to_notion
from ai.summarizer import summarize_youtube
from config import DOWNLOAD_DIR, NOTION_DATABASE_ID, GEMINI_API_KEY

_SUBTITLE_LANGS = ["ko", "en"]

logger = logging.getLogger(__name__)

_active = False
_lock = threading.Lock()


def _ffmpeg_dir() -> str | None:
    """로컬 tools/ffmpeg 우선, 없으면 시스템 PATH 확인."""
    local = os.path.join(os.path.dirname(__file__), "..", "tools", "ffmpeg")
    local = os.path.normpath(local)
    if os.path.exists(os.path.join(local, "ffmpeg.exe")):
        return local
    if shutil.which("ffmpeg"):
        return None  # None이면 yt-dlp가 PATH에서 자동 탐색
    return ""  # 빈 문자열 = ffmpeg 없음


def _has_ffmpeg() -> bool:
    d = _ffmpeg_dir()
    return d is not None and d != ""


def _video_format() -> str:
    if _has_ffmpeg():
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    return "best[ext=mp4]/best[height<=1080]/best"


def _parse_vtt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = []
    for line in raw.split("\n"):
        line = line.strip()
        if (not line or "-->" in line or line.startswith("WEBVTT")
                or line.startswith("Kind:") or line.startswith("Language:")
                or line.isdigit()):
            continue
        if line not in lines[-1:]:
            lines.append(line)
    return " ".join(lines)


def _fetch_subtitles(url: str, base_no_ext: str) -> str:
    """ko/en 자막을 한 번의 요청으로 시도 (수동 자막 우선, 없으면 자동 생성 자막)."""
    try:
        ydl_opts = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": _SUBTITLE_LANGS,
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": base_no_ext,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        logger.warning(f"Subtitle download failed (non-fatal): {e}")

    # base_no_ext에 포함된 [id] 등 glob 특수문자를 이스케이프해야 파일을 제대로 찾는다.
    escaped_base = glob.escape(base_no_ext)
    text = ""
    for lang in _SUBTITLE_LANGS:
        vtt_files = glob.glob(f"{escaped_base}.{lang}*.vtt")
        if vtt_files:
            text = _parse_vtt(vtt_files[0])
            logger.info(f"Subtitles downloaded: lang={lang}")
            break

    for f in glob.glob(f"{escaped_base}.*.vtt"):
        try:
            os.remove(f)
        except OSError:
            pass

    return text


def _download(job: dict) -> tuple[str, str, str, str]:
    """Download video with yt-dlp. Returns (file_path, title, subtitles, description)."""
    job_id = job["id"]
    output_tmpl = os.path.join(DOWNLOAD_DIR, f"%(title)s [%(id)s].%(ext)s")

    final_path = {}

    def postprocessor_hook(d):
        # ffmpeg 병합 완료 후 최종 파일 경로 캡처
        if d["status"] == "finished":
            fp = d.get("info_dict", {}).get("filepath") or d.get("filepath", "")
            if fp:
                final_path["path"] = fp

    ydl_opts = {
        "outtmpl": output_tmpl,
        "format": _video_format(),
        "postprocessor_hooks": [postprocessor_hook],
        "quiet": True,
        "no_warnings": True,
    }
    ffdir = _ffmpeg_dir()
    if ffdir:
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["ffmpeg_location"] = ffdir

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(job["url"], download=True)
        title = info.get("title", f"video_{job_id}")
        description = info.get("description", "")

    # postprocessor_hook → 없으면 prepare_filename 기준으로 탐색
    path = final_path.get("path") or ydl.prepare_filename(info)
    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        for ext in ("mp4", "mkv", "webm", "m4a"):
            candidate = f"{base}.{ext}"
            if os.path.exists(candidate):
                path = candidate
                break

    # 자막은 별도 요청 — 실패해도 진행
    base_no_ext = os.path.splitext(path)[0]
    subtitles = _fetch_subtitles(job["url"], base_no_ext)

    return path, title, subtitles, description


def _build_info_md(title: str, url: str, ai_result) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# {title}",
        "",
        f"- **URL**: {url}",
        f"- **날짜**: {date_str}",
    ]
    if ai_result:
        if ai_result.category:
            lines.append(f"- **카테고리**: {ai_result.category}")
        if ai_result.tags:
            lines.append(f"- **태그**: {', '.join(ai_result.tags)}")
    lines.append("")
    if ai_result and ai_result.summary:
        lines += ["## 요약", "", ai_result.summary, ""]
    return "\n".join(lines)


def _process_job(job: dict, notify_callback=None):
    job_id = job["id"]
    try:
        update_job(job_id, status=STATUS_DOWNLOADING)
        logger.info(f"[job {job_id}] downloading: {job['url']}")

        file_path, title, subtitles, description = _download(job)
        update_job(job_id, status=STATUS_UPLOADING, title=title, file_path=file_path)
        logger.info(f"[job {job_id}] summarizing: {title} (subtitles={'yes' if subtitles else 'no'})")

        ai_result = None
        if GEMINI_API_KEY:
            ai_result = summarize_youtube(subtitles=subtitles, description=description)
            logger.info(f"[job {job_id}] summary done, category={ai_result.category}, tags={ai_result.tags}")

        md_content = _build_info_md(
            title=title,
            url=job["url"],
            ai_result=ai_result,
        )

        logger.info(f"[job {job_id}] uploading: {title}")
        gdrive_link = upload_video_package(file_path, title, md_content)
        update_job(job_id, status=STATUS_NOTIFYING, gdrive_link=gdrive_link)

        notion_url = ""
        if NOTION_DATABASE_ID:
            try:
                notion_url = save_to_notion(
                    database_id=NOTION_DATABASE_ID,
                    job_id=job_id,
                    title=title,
                    url=job["url"],
                    gdrive_link=gdrive_link,
                    summary=ai_result.summary if ai_result else "",
                    tags=ai_result.tags if ai_result else [],
                    category=ai_result.category if ai_result else "",
                )
            except Exception as e:
                logger.warning(f"[job {job_id}] notion failed (non-fatal): {e}")

        update_job(job_id, status=STATUS_DONE, notion_url=notion_url)
        logger.info(f"[job {job_id}] done: {gdrive_link}")

        os.remove(file_path)

        if notify_callback:
            msg = f"✅ 완료: {title}\n📁 Drive: {gdrive_link}"
            if notion_url:
                msg += f"\n📝 Notion: {notion_url}"
            notify_callback(job["chat_id"], msg)

    except Exception as e:
        logger.error(f"[job {job_id}] failed: {e}")
        update_job(job_id, status=STATUS_FAILED, error_msg=str(e))
        if notify_callback:
            notify_callback(job["chat_id"], f"❌ 실패 (job {job_id}): {e}")


def run_worker(notify_callback=None, poll_interval: int = 5):
    global _active
    _active = True
    logger.info("Worker started.")
    while _active:
        with _lock:
            jobs = get_pending_jobs()
        for job in jobs:
            _process_job(job, notify_callback)
        time.sleep(poll_interval)


def stop_worker():
    global _active
    _active = False
