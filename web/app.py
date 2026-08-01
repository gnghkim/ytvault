import time
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from db.models import get_all_jobs, get_job

app = FastAPI(title="YTVault")
templates = Jinja2Templates(directory="web/templates")


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    jobs = get_all_jobs()
    for job in jobs:
        job["created_fmt"] = _fmt_time(job.get("created_at"))
        job["updated_fmt"] = _fmt_time(job.get("updated_at"))
    return templates.TemplateResponse("index.html", {"request": request, "jobs": jobs})


@app.get("/api/jobs")
async def api_jobs():
    jobs = get_all_jobs()
    for job in jobs:
        job["created_fmt"] = _fmt_time(job.get("created_at"))
        job["updated_fmt"] = _fmt_time(job.get("updated_at"))
    return jobs


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: int):
    job = get_job(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found")
    job["created_fmt"] = _fmt_time(job.get("created_at"))
    job["updated_fmt"] = _fmt_time(job.get("updated_at"))
    return job
