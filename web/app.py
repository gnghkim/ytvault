import os
import time
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from db.models import get_all_jobs, get_job
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = [_prepare_job(job) for job in get_all_jobs()]
    return templates.TemplateResponse("index.html", {"request": request, "jobs": jobs})


@app.get("/api/jobs")
async def api_jobs():
    return [_prepare_job(job) for job in get_all_jobs()]


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int):
    job = get_job(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")
    return _prepare_job(job)
