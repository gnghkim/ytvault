import glob
import os
import shutil
import time
import logging
import threading
from datetime import datetime
import yt_dlp
import health_state
from log_redaction import redact_secrets
from db.models import (
    expire_resolution_choices,
    get_pending_jobs,
    request_resolution_choice,
    set_stage,
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
_DOWNLOAD_ATTEMPTS = 3

# 최대 해상도가 이보다 높은 영상은 텔레그램으로 해상도를 묻는다.
# RESOLUTION_CHOICE_TIMEOUT 안에 답이 없으면 이 해상도로 받는다.
DEFAULT_MAX_HEIGHT = 1080
RESOLUTION_CHOICE_TIMEOUT = 10 * 60
_CHOICE_LEVELS = (720, 1080, 1440, 2160, 4320)

# 웹에 보여 줄 세부 진행 단계 (jobs.stage)
STAGE_PROBING = "probing"
STAGE_DOWNLOADING = "downloading"
STAGE_MERGING = "merging"
STAGE_SUBTITLES = "subtitles"
STAGE_SUMMARIZING = "summarizing"
STAGE_UPLOADING = "uploading"
STAGE_NOTION = "notion"

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
    return d != ""


def _js_runtimes() -> dict:
    """Enable an installed JS runtime for YouTube challenge solving."""
    for runtime, executable in (("deno", "deno"), ("node", "node"), ("quickjs", "qjs")):
        path = shutil.which(executable)
        if path:
            return {runtime: {"path": path}}
    return {}


def _youtube_opts() -> dict:
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": logger,
        "no_warnings": False,
    }
    if js_runtimes := _js_runtimes():
        opts["js_runtimes"] = js_runtimes
    return opts


def _video_format() -> str:
    if _has_ffmpeg():
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    return "best[ext=mp4]/best[height<=1080]/best"


def resolution_label(height: int) -> str:
    return {2160: "4K", 4320: "8K"}.get(height, f"{height}p")


def _short_side(fmt: dict) -> int:
    """세로 영상(1080x1920)도 1080p로 보도록 짧은 변을 해상도로 쓴다. yt-dlp의 res 정렬과 같은 기준."""
    return min(filter(None, (fmt.get("height"), fmt.get("width"))), default=0)


def _format_size(fmt: dict) -> int:
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def _resolution_options(info: dict) -> list[tuple[int, int]]:
    """고를 수 있는 (해상도, 예상 바이트) 목록. 최대 해상도가 기본값 이하면 묻지 않으므로 빈 목록."""
    formats = info.get("formats") or []
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and _short_side(f)]
    if not videos:
        return []
    top = max(_short_side(f) for f in videos)
    if top <= DEFAULT_MAX_HEIGHT:
        return []
    audio = max(
        (_format_size(f) for f in formats
         if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")),
        default=0,
    )
    options = {}
    for level in [lv for lv in _CHOICE_LEVELS if lv < top] + [top]:
        candidates = [f for f in videos if _short_side(f) <= level]
        if not candidates:
            continue
        res = max(_short_side(f) for f in candidates)
        at_res = [f for f in candidates if _short_side(f) == res]
        preferred = [f for f in at_res if f.get("ext") == "mp4"] or at_res  # _video_format()과 같은 우선순위
        # yt-dlp는 formats를 나쁜 것→좋은 것 순으로 정렬해 두므로, 같은 해상도에서는 마지막 것을 고른다
        size = _format_size(preferred[-1])
        options[res] = size + audio if size else 0
    return sorted(options.items())


def _fmt_bytes(n: float) -> str:
    return f"{n / 2**30:.1f}GB" if n >= 2**30 else f"{n / 2**20:.0f}MB"


def _fmt_eta(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}분 남음" if seconds >= 60 else f"{seconds}초 남음"


def _transfer_detail(done: float, total: float, speed: float | None, eta: float | None) -> str:
    parts = [f"{_fmt_bytes(done)} / {_fmt_bytes(total)}" if total else _fmt_bytes(done)]
    if speed:
        parts.append(f"{speed / 2**20:.1f}MB/s")
    if eta is not None:
        parts.append(_fmt_eta(eta))
    return " · ".join(parts)


class _StageReporter:
    """웹에 보여 줄 세부 단계를 DB에 기록한다. 같은 단계의 진행률은 몇 초에 한 번만 쓴다."""

    def __init__(self, job_id: int, min_interval: float = 2.0):
        self.job_id = job_id
        self.min_interval = min_interval
        self._stage = None
        self._written = 0.0

    def __call__(self, stage: str, progress: float | None = None, detail: str | None = None):
        now = time.monotonic()
        if stage == self._stage and now - self._written < self.min_interval:
            return
        self._stage, self._written = stage, now
        try:
            set_stage(self.job_id, stage, None if progress is None else round(progress, 1), detail)
        except Exception as e:  # 진행 표시가 실패해도 작업은 계속한다
            logger.warning("Could not record stage for job %s: %s", self.job_id, e)


def _noop_report(stage, progress=None, detail=None):
    pass


def _with_403_retry(job_id: int, call):
    """YouTube가 가끔 돌려주는 일시적 403이면 잠시 쉬었다가 다시 시도한다."""
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            return call()
        except yt_dlp.utils.DownloadError as exc:
            is_transient_403 = "HTTP Error 403" in str(exc)
            if not is_transient_403 or attempt == _DOWNLOAD_ATTEMPTS:
                raise
            delay = 2 ** (attempt - 1)
            logger.warning(
                f"[job {job_id}] YouTube returned 403; "
                f"retrying extraction in {delay}s ({attempt}/{_DOWNLOAD_ATTEMPTS})"
            )
            time.sleep(delay)


def _probe(job: dict) -> dict:
    """다운로드 없이 영상 정보(제목, 포맷 목록)만 가져온다."""
    def extract():
        with yt_dlp.YoutubeDL(_youtube_opts()) as ydl:
            return ydl.extract_info(job["url"], download=False)
    return _with_403_retry(job["id"], extract)


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
    """Try Korean first, then English only when no usable Korean text exists."""
    escaped_base = glob.escape(base_no_ext)
    text = ""
    for lang in _SUBTITLE_LANGS:
        ydl_opts = {
            **_youtube_opts(),
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [lang],
            "subtitlesformat": "vtt",
            "skip_download": True,
            "outtmpl": base_no_ext,
        }
        rate_limited = False
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception as e:
            rate_limited = "HTTP Error 429" in str(e)
            logger.warning("Subtitle download failed (non-fatal, lang=%s): %s", lang, redact_secrets(e))

        vtt_path = f"{base_no_ext}.{lang}.vtt"
        if os.path.isfile(vtt_path):
            try:
                text = _parse_vtt(vtt_path)
            except (OSError, UnicodeError) as e:
                logger.warning("Subtitle read failed (non-fatal): %s", redact_secrets(e))
        if text:
            logger.info("Subtitles downloaded: lang=%s", lang)
            break
        if rate_limited:
            logger.warning("Subtitle rate limit reached; continuing with video description.")
            break

    for path in glob.glob(f"{escaped_base}.*.vtt"):
        try:
            os.remove(path)
        except OSError:
            pass
    return text


def _download(job: dict, report=_noop_report) -> tuple[str, str, str, str]:
    """Download video with yt-dlp. Returns (file_path, title, subtitles, description)."""
    job_id = job["id"]
    # 리눅스 파일명 한도는 255바이트(한글 1자 = 3바이트)라 제목을 바이트 기준으로 자른다.
    # 150B + " [id]" + ".f399.mp4.part" / ".ko.vtt" 를 붙여도 한도 안에 들어간다.
    output_tmpl = os.path.join(DOWNLOAD_DIR, "%(title).150B [%(id)s].%(ext)s")

    final_path = {}
    streams = {}  # 파일별 (받은 바이트, 전체 바이트). 영상과 오디오를 따로 받는다

    def progress_hook(d):
        if d["status"] not in ("downloading", "finished"):
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or (total if d["status"] == "finished" else 0)
        streams[d.get("filename")] = (done, total)
        got = sum(a for a, _ in streams.values())
        size = sum(b for _, b in streams.values())
        report(STAGE_DOWNLOADING, got / size * 100 if size else None,
               _transfer_detail(got, size, d.get("speed"), d.get("eta")))

    def postprocessor_hook(d):
        if d["status"] == "started" and d.get("postprocessor") == "Merger":
            report(STAGE_MERGING)
        # ffmpeg 병합 완료 후 최종 파일 경로 캡처
        if d["status"] == "finished":
            fp = d.get("info_dict", {}).get("filepath") or d.get("filepath", "")
            if fp:
                final_path["path"] = fp

    ydl_opts = {
        **_youtube_opts(),
        "outtmpl": output_tmpl,
        "format": _video_format(),
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
    }
    if max_height := job.get("max_height"):
        # res = 짧은 변 기준. 이 해상도 이하에서 가장 좋은 포맷을 고른다
        ydl_opts["format_sort"] = [f"res:{max_height}"]
    ffdir = _ffmpeg_dir()
    if ffdir != "":
        ydl_opts["merge_output_format"] = "mp4"
    if ffdir:
        ydl_opts["ffmpeg_location"] = ffdir

    def download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
            return info, ydl.prepare_filename(info)

    info, prepared_path = _with_403_retry(job_id, download)

    title = info.get("title", f"video_{job_id}")
    description = info.get("description", "")

    # postprocessor_hook → 없으면 prepare_filename 기준으로 탐색
    path = final_path.get("path") or prepared_path
    if not os.path.exists(path):
        base = os.path.splitext(path)[0]
        for ext in ("mp4", "mkv", "webm", "m4a"):
            candidate = f"{base}.{ext}"
            if os.path.exists(candidate):
                path = candidate
                break

    # 자막은 별도 요청 — 실패해도 진행
    base_no_ext = os.path.splitext(path)[0]
    report(STAGE_SUBTITLES)
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


def _discard_download(file_path: str | None) -> None:
    """실패한 작업의 영상 파일을 지운다. 재시도하면 처음부터 다시 받는다."""
    if not file_path:
        return
    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove %s: %s", file_path, e)


def _ask_resolution(job: dict, info: dict, options: list[tuple[int, int]], notify_callback) -> None:
    """해상도 선택 버튼을 보내고 job을 선택 대기 상태로 둔다."""
    job_id = job["id"]
    title = info.get("title") or job["url"]
    update_job(job_id, title=title)
    request_resolution_choice(job_id, time.time() + RESOLUTION_CHOICE_TIMEOUT)
    duration = info.get("duration")
    length = f" ({int(duration) // 60}분 {int(duration) % 60}초)" if duration else ""
    buttons = [
        (f"{resolution_label(res)} · 약 {_fmt_bytes(size)}" if size else resolution_label(res),
         f"res:{job_id}:{res}")
        for res, size in options
    ]
    notify_callback(
        job["chat_id"],
        f"🎬 {title}{length}\n"
        f"최대 {resolution_label(options[-1][0])} 영상입니다. 저장할 해상도를 골라 주세요.\n"
        f"{RESOLUTION_CHOICE_TIMEOUT // 60}분 안에 고르지 않으면 {resolution_label(DEFAULT_MAX_HEIGHT)}로 저장합니다.",
        buttons=buttons,
    )
    logger.info(f"[job {job_id}] waiting for resolution choice: {[res for res, _ in options]}")


def _process_job(job: dict, notify_callback=None):
    job_id = job["id"]
    file_path = None
    report = _StageReporter(job_id)
    try:
        update_job(job_id, status=STATUS_DOWNLOADING)

        if job.get("max_height") is None:
            report(STAGE_PROBING)
            info = _probe(job)
            options = _resolution_options(info)
            if options and notify_callback:
                _ask_resolution(job, info, options, notify_callback)
                return
            # 1080p 이하 영상은 제한 없이, 물어볼 수 없으면 기본 해상도로 받는다
            job = {**job, "max_height": DEFAULT_MAX_HEIGHT if options else 0}
            update_job(job_id, max_height=job["max_height"], title=info.get("title"))

        logger.info(f"[job {job_id}] downloading: {job['url']} (max_height={job['max_height'] or 'best'})")
        report(STAGE_DOWNLOADING, 0)
        file_path, title, subtitles, description = _download(job, report)
        update_job(job_id, status=STATUS_UPLOADING, title=title, file_path=file_path)
        logger.info(f"[job {job_id}] summarizing: {title} (subtitles={'yes' if subtitles else 'no'})")

        ai_result = None
        if GEMINI_API_KEY:
            report(STAGE_SUMMARIZING)
            ai_result = summarize_youtube(subtitles=subtitles, description=description)
            logger.info(f"[job {job_id}] summary done, category={ai_result.category}, tags={ai_result.tags}")

        md_content = _build_info_md(
            title=title,
            url=job["url"],
            ai_result=ai_result,
        )

        logger.info(f"[job {job_id}] uploading: {title}")
        report(STAGE_UPLOADING, 0)
        total_bytes = os.path.getsize(file_path)
        upload_started = time.monotonic()

        def upload_progress(fraction: float):
            sent = total_bytes * fraction
            elapsed = time.monotonic() - upload_started
            speed = sent / elapsed if elapsed > 0 and sent else None
            eta = (total_bytes - sent) / speed if speed else None
            report(STAGE_UPLOADING, fraction * 100, _transfer_detail(sent, total_bytes, speed, eta))

        gdrive_link = upload_video_package(file_path, title, md_content, on_progress=upload_progress)
        update_job(job_id, status=STATUS_NOTIFYING, gdrive_link=gdrive_link)

        notion_url = ""
        if NOTION_DATABASE_ID:
            report(STAGE_NOTION)
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

        update_job(job_id, status=STATUS_DONE, notion_url=notion_url, stage=None, progress=None, stage_detail=None)
        logger.info(f"[job {job_id}] done: {gdrive_link}")

        os.remove(file_path)

        if notify_callback:
            msg = f"✅ 완료: {title}\n📁 Drive: {gdrive_link}"
            if notion_url:
                msg += f"\n📝 Notion: {notion_url}"
            notify_callback(job["chat_id"], msg)

    except Exception as e:
        error_msg = redact_secrets(e)
        logger.error(f"[job {job_id}] failed: {error_msg}")
        update_job(job_id, status=STATUS_FAILED, error_msg=error_msg, stage=None, progress=None, stage_detail=None)
        _discard_download(file_path)
        if notify_callback:
            notify_callback(job["chat_id"], f"❌ 실패 (job {job_id}): {error_msg}")


def _expire_resolution_choices(notify_callback=None) -> None:
    for job in expire_resolution_choices(time.time(), DEFAULT_MAX_HEIGHT):
        label = resolution_label(DEFAULT_MAX_HEIGHT)
        logger.info(f"[job {job['id']}] no resolution chosen in time; using {label}")
        if notify_callback:
            notify_callback(
                job["chat_id"],
                f"⏰ {RESOLUTION_CHOICE_TIMEOUT // 60}분 동안 선택이 없어 {label}로 저장합니다: {job.get('title') or job['url']}",
            )


def run_worker(notify_callback=None, poll_interval: int = 5):
    global _active
    _active = True
    logger.info("Worker started.")
    while _active:
        # 대기열 확인에서 난 예외(DB 잠김 등)로 워커 스레드가 조용히 죽지 않게 한다.
        # 연속 오류 횟수는 헬스체커가 본다.
        try:
            with _lock:
                _expire_resolution_choices(notify_callback)
                jobs = get_pending_jobs()
            health_state.worker_tick()
            for job in jobs:
                health_state.worker_job_started(job["id"])
                try:
                    _process_job(job, notify_callback)
                finally:
                    health_state.worker_job_finished()
        except Exception as e:
            logger.error(f"Worker loop error: {redact_secrets(e)}")
            health_state.worker_loop_error(e)
        time.sleep(poll_interval)


def stop_worker():
    global _active
    _active = False
