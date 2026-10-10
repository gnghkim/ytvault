import math
import os
import time
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from db.models import count_jobs, count_jobs_by_status, get_job, search_jobs
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
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 100


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


def _job_page(page: int, size: int, query: str) -> dict:
    """검색어에 맞는 작업의 한 페이지. 범위를 벗어난 페이지 번호는 가장 가까운 페이지로 맞춘다."""
    size = min(max(size, 1), MAX_PAGE_SIZE)
    query = query.strip()
    total = count_jobs(query)
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 1), pages)
    items = search_jobs(query, limit=size, offset=(page - 1) * size)
    return {
        "items": [_prepare_job(job) for job in items],
        "total": total,
        "page": page,
        "pages": pages,
        "size": size,
        "query": query,
        "counts": count_jobs_by_status(),  # 통계 카드는 검색과 무관하게 전체 기준
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1, q: str = Query("", max_length=MAX_QUERY_LENGTH)):
    return templates.TemplateResponse(request, "index.html", {"initial": _job_page(page, PAGE_SIZE, q)})


@app.get("/api/jobs")
async def api_jobs(
    page: int = 1,
    size: int = PAGE_SIZE,
    q: str = Query("", max_length=MAX_QUERY_LENGTH),
):
    return _job_page(page, size, q)


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int):
    job = get_job(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")
    return _prepare_job(job)
