import logging
import math
import os
import time
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from db.models import (
    count_jobs,
    count_jobs_by_status,
    get_job,
    retry_failed_job,
    search_jobs,
    STATUS_AWAITING_CHOICE,
    STATUS_DONE,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_NOTIFYING,
    STATUS_PENDING,
    STATUS_UPLOADING,
)
from log_redaction import redact_secrets
from web.auth import require_auth

# Every route requires credentials. The auto-generated docs are turned off
# because FastAPI serves them outside app-level dependencies.
app = FastAPI(
    title="YTVault",
    dependencies=[Depends(require_auth)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 100
# 상단 통계 카드로 거르는 상태 묶음 (?status=). 알 수 없는 값이면 거르지 않는다
STATUS_FILTERS = {
    "done": [STATUS_DONE],
    "active": [STATUS_DOWNLOADING, STATUS_UPLOADING, STATUS_NOTIFYING],
    "awaiting_choice": [STATUS_AWAITING_CHOICE],
    "pending": [STATUS_PENDING],
    "failed": [STATUS_FAILED],
}
# 상태를 바꾸는 요청에 붙여야 하는 헤더. 브라우저는 Basic 인증 정보를 다른 사이트가 보낸 폼 POST에도
# 자동으로 붙이지만, 다른 사이트는 이 헤더를 붙일 수 없다(붙이려면 CORS 사전 요청이 필요하고 허용하지 않는다).
CSRF_HEADER = "X-YTVault-Request"


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _prepare_job(job: dict) -> dict:
    job = dict(job)
    job.pop("chat_id", None)
    job.pop("file_path", None)
    job["created_fmt"] = _fmt_time(job.get("created_at"))
    job["updated_fmt"] = _fmt_time(job.get("updated_at"))
    # 브라우저 시계와 무관하게 남은 시간을 서버 기준으로 계산해 준다
    deadline = job.pop("choice_deadline", None)
    job["choice_remaining"] = max(0, int(deadline - time.time())) if deadline else None
    if job.get("error_msg"):
        job["error_msg"] = redact_secrets(job["error_msg"])
    return job


def _job_page(page: int, size: int, query: str, status: str = "") -> dict:
    """검색어·상태에 맞는 작업의 한 페이지. 범위를 벗어난 페이지 번호는 가장 가까운 페이지로 맞춘다."""
    size = min(max(size, 1), MAX_PAGE_SIZE)
    query = query.strip()
    status = status if status in STATUS_FILTERS else ""
    statuses = STATUS_FILTERS.get(status)
    total = count_jobs(query, statuses)
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 1), pages)
    items = search_jobs(query, limit=size, offset=(page - 1) * size, statuses=statuses)
    return {
        "items": [_prepare_job(job) for job in items],
        "total": total,
        "page": page,
        "pages": pages,
        "size": size,
        "query": query,
        "status": status,
        "counts": count_jobs_by_status(),  # 통계 카드는 검색과 무관하게 전체 기준
    }


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    page: int = 1,
    q: str = Query("", max_length=MAX_QUERY_LENGTH),
    status: str = "",
):
    return templates.TemplateResponse(request, "index.html", {"initial": _job_page(page, PAGE_SIZE, q, status)})


@app.get("/api/jobs")
async def api_jobs(
    page: int = 1,
    size: int = PAGE_SIZE,
    q: str = Query("", max_length=MAX_QUERY_LENGTH),
    status: str = "",
):
    return _job_page(page, size, q, status)


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _prepare_job(job)


@app.post("/api/jobs/{job_id}/retry")
async def api_retry_job(job_id: int, csrf: str | None = Header(None, alias=CSRF_HEADER)):
    """실패한 작업을 다시 대기열에 넣는다. 워커가 몇 초 안에 가져간다."""
    if csrf != "1":
        raise HTTPException(status_code=403, detail="Missing request header")
    if not retry_failed_job(job_id):
        job = get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        raise HTTPException(status_code=409, detail=f"Only failed jobs can be retried (status: {job['status']})")
    logger.info(f"[job {job_id}] retry requested from the web UI")
    return _prepare_job(get_job(job_id))
