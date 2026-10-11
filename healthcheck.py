"""ytvault 헬스체커. systemd 타이머가 1분마다 실행해 결과를 Uptime Kuma Push 모니터로 보낸다.

ytvault와 별개의 프로세스로, 다음을 읽기만 한다(쓰거나 지우지 않는다).
- health_state.json: 워커가 대기열을 확인한 시각, 봇의 텔레그램 폴링 성공 시각 (health_state.py가 기록)
- ytvault.db: 작업 큐와 진행 단계, 실패 기록 (읽기 전용으로 연다)
- 다운로드 폴더와 디스크 여유 공간, token.json 존재 여부
- 웹 대시보드: 로그인 없이 요청해 401(로그인 요구)이 오는지만 본다

사용법:
    python healthcheck.py             # 판정하고 Kuma로 보낸다
    python healthcheck.py --no-push   # 판정 결과만 출력
    python healthcheck.py --json      # 판정에 쓴 값까지 출력
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import healthcheck_config

HEALTHY = "HEALTHY"
IDLE = "IDLE"
DEGRADED = "DEGRADED"
UNHEALTHY = "UNHEALTHY"

_ACTIVE_STATUSES = ("downloading", "uploading", "notifying")
_MAX_MSG_LENGTH = 250
# 같은 기기의 웹 대시보드는 프록시 환경변수와 관계없이 직접 연결한다
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 실패 메시지로 원인을 가린다. drive_*는 사람이 고치기 전까지 모든 업로드가 실패하는 종류
_FAILURE_KINDS = (
    ("drive_auth", (
        "invalid_grant", "Token has been expired or revoked", "Google Drive 인증 토큰",
        "unauthorized_client", "invalid_client", "invalid authentication credentials",
    )),
    ("drive_storage", ("storageQuotaExceeded",)),
    ("disk_full", ("No space left on device", "Errno 28", "WinError 112")),
)
_FAILURE_LABELS = {
    "drive_auth": "Drive authentication error",
    "drive_storage": "Drive storage full",
    "disk_full": "disk full",
}


@dataclass
class Report:
    state: str
    kuma_status: str
    msg: str
    ping: float | None = None
    findings: list = field(default_factory=list)  # [(심각도, 설명)]


# --------------------------------------------------------------------------
# 수집
# --------------------------------------------------------------------------

def read_state(path: str) -> tuple[dict | None, str | None]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as e:
        return None, f"unreadable ({type(e).__name__})"


def classify_failure(error_msg: str | None) -> str | None:
    text = error_msg or ""
    for kind, patterns in _FAILURE_KINDS:
        if any(p in text for p in patterns):
            return kind
    return None


def read_db(path: str, now: float, failure_window: float) -> dict:
    """작업 큐 현황. 읽기 전용으로 열어 운영 DB를 바꾸지 않는다."""
    try:
        conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        return {"error": str(e)}
    try:
        conn.row_factory = sqlite3.Row
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())
        # 대기에 들어간 시각: 재시도로 다시 대기열에 들어가면 updated_at이 바뀐다
        oldest_pending = conn.execute(
            "SELECT MIN(updated_at) FROM jobs WHERE status = 'pending'"
        ).fetchone()[0]
        active = [dict(r) for r in conn.execute(
            "SELECT id, status, stage, progress, updated_at FROM jobs"
            f" WHERE status IN ({', '.join('?' * len(_ACTIVE_STATUSES))})",
            _ACTIVE_STATUSES,
        )]
        last_done = conn.execute("SELECT MAX(updated_at) FROM jobs WHERE status = 'done'").fetchone()[0]
        # 마지막 성공 이후의 실패 (최신순)
        failures = [dict(r) for r in conn.execute(
            "SELECT id, error_msg, updated_at FROM jobs WHERE status = 'failed' AND updated_at > ?"
            " ORDER BY updated_at DESC LIMIT 50",
            (last_done or 0,),
        )]
        recent_failed = [dict(r) for r in conn.execute(
            "SELECT id, error_msg, updated_at FROM jobs WHERE status = 'failed' AND updated_at >= ?"
            " ORDER BY updated_at DESC",
            (now - failure_window,),
        )]
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()
    return {
        "counts": counts,
        "oldest_pending": oldest_pending,
        "active": active,
        "last_done": last_done,
        "failures_since_done": failures,
        "recent_failed": recent_failed,
    }


def read_disk(download_dir: str) -> dict:
    try:
        usage = shutil.disk_usage(download_dir)
    except OSError as e:
        return {"error": str(e)}
    files, size = 0, 0
    for root, _, names in os.walk(download_dir):
        for name in names:
            try:
                size += os.path.getsize(os.path.join(root, name))
                files += 1
            except OSError:
                pass  # 그 사이 지워진 파일
    return {
        "free": usage.free,
        "total": usage.total,
        "used_pct": usage.used / usage.total * 100 if usage.total else 0,
        "download_files": files,
        "download_bytes": size,
    }


def read_drive_token(path: str) -> dict:
    """token.json이 있는지, 갱신 토큰이 들어 있는지만 본다. 값은 읽어 두지 않는다."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"exists": True, "mtime": os.path.getmtime(path), "refreshable": bool(data.get("refresh_token"))}
    except FileNotFoundError:
        return {"exists": False}
    except (OSError, ValueError):
        return {"exists": True, "mtime": None, "refreshable": False}


def check_web(host: str, port: int, timeout: float) -> dict:
    """로그인 없이 요청한다. 401이 오면 웹서버와 인증이 모두 살아 있는 것."""
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    started = time.monotonic()
    try:
        with _LOCAL_OPENER.open(f"http://{host}:{port}/", timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (urllib.error.URLError, OSError) as e:
        return {"code": None, "error": str(getattr(e, "reason", e)), "ms": None}
    return {"code": code, "error": None, "ms": (time.monotonic() - started) * 1000}


def collect(now: float, cfg=healthcheck_config) -> dict:
    state, state_error = read_state(cfg.STATE_PATH)
    return {
        "state": state,
        "state_error": state_error,
        "db": read_db(cfg.DB_PATH, now, cfg.FAILURE_WINDOW),
        "disk": read_disk(cfg.DOWNLOAD_DIR),
        "drive_token": read_drive_token(cfg.DRIVE_TOKEN_PATH),
        "web": check_web(cfg.WEB_HOST, cfg.WEB_PORT, cfg.WEB_TIMEOUT),
    }


# --------------------------------------------------------------------------
# 판정
# --------------------------------------------------------------------------

def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 2 * 3600:
        return f"{seconds // 60}m"
    if seconds < 2 * 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _gb(n: float) -> str:
    return f"{n / 2**30:.1f}GB" if n < 10 * 2**30 else f"{n / 2**30:.0f}GB"


def _short(text: str | None, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def evaluate(facts: dict, now: float, cfg=healthcheck_config) -> Report:
    findings: list[tuple[str, str]] = []
    summary: list[str] = []

    def bad(text):
        findings.append((UNHEALTHY, text))

    def warn(text):
        findings.append((DEGRADED, text))

    state = facts.get("state")
    db = facts.get("db") or {}
    db_ok = "error" not in db
    active_by_id = {job["id"]: job for job in db.get("active", [])} if db_ok else {}

    bot_problem = worker_problem = False
    worker_idle = False  # 워커가 쉬면서 대기열을 계속 확인하고 있는가
    current_job = None

    # --- 봇과 워커 (상태 파일) ---------------------------------------------
    if state is None:
        bad(f"no health state ({facts.get('state_error') or 'missing'}): ytvault not running?")
        bot_problem = worker_problem = True
    else:
        started = state.get("started_at") or 0
        in_grace = now - started < cfg.STARTUP_GRACE
        bot = state.get("bot") or {}
        worker = state.get("worker") or {}

        # 봇: 새 메시지가 없어도 롱 폴링은 계속 성공하므로 마지막 성공 시각으로 판단한다
        last_ok = bot.get("last_ok")
        bot_age = now - (last_ok or started)
        bot_dead = bot_age >= cfg.BOT_POLL_FAIL
        bot_finding = None
        error_kind = bot.get("last_error_kind")
        error_is_latest = bool(bot.get("last_error_at")) and bot["last_error_at"] > (last_ok or 0)
        if error_is_latest and error_kind == "auth":
            bot_finding = (UNHEALTHY, "bot token rejected by Telegram")  # 토큰을 고치기 전에는 회복되지 않는다
        elif bot_age >= cfg.BOT_POLL_WARN and not (in_grace and last_ok is None):
            reason = {
                "conflict": "another instance is polling",
                "rate_limit": "rate limited",
            }.get(error_kind) if error_is_latest else None
            if not reason and error_is_latest:
                reason = _short(bot.get("last_error"), 40)
            text = f"bot polling down {_ago(bot_age)}" + (f" ({reason})" if reason else "")
            bot_finding = (UNHEALTHY if bot_dead else DEGRADED, text)
        bot_problem = bot_finding is not None

        # 워커
        current_job = worker.get("current_job")
        if current_job is not None:
            job = active_by_id.get(current_job)
            progress_at = max(filter(None, (worker.get("busy_since"), job and job.get("updated_at"))), default=started)
            stall = now - progress_at
            stage = (job and (job.get("stage") or job.get("status"))) or "working"
            if stall >= cfg.JOB_STALL_FAIL:
                bad(f"job #{current_job} stalled {_ago(stall)} ({stage})")
                worker_problem = True
            elif stall >= cfg.JOB_STALL_WARN:
                warn(f"job #{current_job} no progress {_ago(stall)} ({stage})")
                worker_problem = True
            text = f"{stage} #{current_job}"
            if job and job.get("progress") is not None:
                text += f" {job['progress']:.0f}%"
            summary.append(text)
            if not worker_problem:
                summary.append("progress OK")
        else:
            last_tick = worker.get("last_tick")
            tick_age = now - (last_tick or started)
            if tick_age >= cfg.WORKER_TICK_FAIL and not (in_grace and last_tick is None):
                if bot_dead and error_kind != "auth":
                    # 둘 다 멈췄으면 프로세스 자체가 멈춘 것으로 한 줄로 알린다
                    bot_finding = None
                    bad(f"ytvault not responding {_ago(min(tick_age, bot_age))} (stopped or hung)")
                else:
                    bad(f"worker stalled {_ago(tick_age)}")
                worker_problem = True
            else:
                worker_idle = True
        if bot_finding:
            findings.insert(0, bot_finding)

        loop_errors = worker.get("loop_errors") or 0
        if loop_errors:
            text = f"worker error x{loop_errors}: {_short(worker.get('last_error'), 50)}"
            (bad if loop_errors >= cfg.WORKER_ERRORS_FAIL else warn)(text)
            worker_problem = True

    # --- 작업 큐 (DB) --------------------------------------------------------
    pending = awaiting = 0
    if not db_ok:
        bad(f"database unreadable: {_short(db['error'], 50)}")
    else:
        counts = db.get("counts") or {}
        pending = counts.get("pending", 0)
        awaiting = counts.get("awaiting_choice", 0)

        if pending and db.get("oldest_pending"):
            wait = now - db["oldest_pending"]
            if worker_idle and wait >= cfg.QUEUE_STUCK:
                bad(f"queue not processed {_ago(wait)}")
            elif wait >= cfg.QUEUE_WAIT_WARN:
                warn(f"queue waiting {_ago(wait)}")

        # 워커가 맡고 있지 않은데 진행 중 상태로 남은 작업 (워커가 처리 도중 죽은 흔적)
        if state is not None:
            for job in db.get("active", []):
                if job["id"] != current_job and now - (job.get("updated_at") or 0) >= cfg.WORKER_TICK_FAIL:
                    warn(f"job #{job['id']} stuck in {job['status']}")

        # 사람이 고치기 전까지 계속 실패하는 원인 (마지막 성공 이후, 기간 제한 없음)
        token = facts.get("drive_token") or {}
        for failure in db.get("failures_since_done", []):
            kind = classify_failure(failure.get("error_msg"))
            if kind not in ("drive_auth", "drive_storage"):
                continue
            label = f"{_FAILURE_LABELS[kind]} (job #{failure['id']})"
            renewed = kind == "drive_auth" and (token.get("mtime") or 0) > failure["updated_at"]
            if renewed:
                warn(f"{label}; token renewed, waiting for next upload")
            else:
                bad(label)
            break

        # 반복 실패와 일시적 실패
        window_start = now - cfg.FAILURE_WINDOW
        streak = [f for f in db.get("failures_since_done", []) if f["updated_at"] >= window_start]
        recent = db.get("recent_failed", [])
        if len(streak) >= cfg.FAILURE_STREAK_FAIL:
            bad(f"{len(streak)} jobs failed in a row (last #{streak[0]['id']})")
        elif recent:
            last = recent[0]
            kind = classify_failure(last.get("error_msg"))
            reason = _FAILURE_LABELS.get(kind) or _short(last.get("error_msg"), 50)
            warn(f"{len(recent)} failed in {_ago(cfg.FAILURE_WINDOW)} (last #{last['id']}: {reason})")

    # --- Drive 토큰 (파일만 확인, 네트워크 요청 없음) -----------------------
    token = facts.get("drive_token") or {}
    if not token.get("exists"):
        bad("Drive token missing")
    elif not token.get("refreshable"):
        warn("Drive token has no refresh token")

    # --- 저장장치 ------------------------------------------------------------
    disk = facts.get("disk") or {}
    findings_before_disk = len(findings)
    if "error" in disk:
        bad(f"download dir unavailable: {_short(disk['error'], 50)}")
    else:
        free = disk["free"]
        if free < cfg.DISK_FREE_FAIL_GB * 2**30:
            bad(f"disk low: {_gb(free)} free")
        elif free < cfg.DISK_FREE_WARN_GB * 2**30 or disk["used_pct"] >= cfg.DISK_USAGE_WARN_PCT:
            warn(f"disk {_gb(free)} free ({disk['used_pct']:.0f}% used)")
    disk_ok = len(findings) == findings_before_disk  # 문제가 있으면 요약에 여유 공간을 또 쓰지 않는다
    # 처리 중이 아닌데 다운로드 폴더에 남은 파일 (중단된 작업의 잔여물). 지우지는 않는다
    if current_job is None and disk.get("download_files"):
        warn(f"leftover files {disk['download_files']} ({_gb(disk['download_bytes'])})")

    # --- 웹 대시보드 ---------------------------------------------------------
    web = facts.get("web") or {}
    code = web.get("code")
    if code is None:
        warn(f"web UI not responding ({_short(web.get('error'), 40)})")
    elif code == 503:
        warn("web UI disabled (WEB_PASSWORD empty)")
    elif 200 <= code < 300:
        warn("web UI answers without login")
    elif code != 401:
        warn(f"web UI HTTP {code}")

    # --- 종합 ----------------------------------------------------------------
    severities = {s for s, _ in findings}
    busy = current_job is not None or pending > 0 or bool(db_ok and db.get("active"))
    if UNHEALTHY in severities:
        overall = UNHEALTHY
    elif DEGRADED in severities:
        overall = DEGRADED
    elif busy:
        overall = HEALTHY
    else:
        overall = IDLE

    if db_ok and current_job is None and not pending:
        summary.insert(0, "no pending jobs")
    if not bot_problem:
        summary.append("bot OK")
    if not worker_problem and current_job is None:
        summary.append("worker OK")
    if db_ok and (pending or awaiting or overall in (UNHEALTHY, DEGRADED)):
        queue = f"queue {pending}"
        if awaiting:
            queue += f" (+{awaiting} awaiting choice)"
        summary.append(queue)
    if disk_ok:
        summary.append(f"disk {_gb(disk['free'])} free")

    problems = [t for s, t in findings if s == UNHEALTHY] + [t for s, t in findings if s == DEGRADED]
    msg = " | ".join([overall] + problems + summary)
    if len(msg) > _MAX_MSG_LENGTH:
        msg = msg[:_MAX_MSG_LENGTH - 1] + "…"

    if overall == UNHEALTHY:
        kuma_status = "down"
    elif overall == DEGRADED:
        kuma_status = cfg.DEGRADED_STATUS
    else:
        kuma_status = "up"
    return Report(overall, kuma_status, msg, web.get("ms"), findings)


# --------------------------------------------------------------------------
# Uptime Kuma Push
# --------------------------------------------------------------------------

def push(url: str, status: str, msg: str, ping: float | None, timeout: float) -> tuple[bool, str]:
    """한 번만 보낸다. 실패해도 다시 시도하지 않고, 다음 실행(1분 뒤)에 맡긴다.

    URL에는 Push 토큰이 들어 있으므로 결과 설명에는 호스트만 남긴다.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, "YTVAULT_KUMA_PUSH_URL is not a valid http(s) URL"
    query = urlencode({"status": status, "msg": msg, "ping": "" if ping is None else round(ping)})
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            body = resp.read(4096)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} from {parts.netloc}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"{parts.netloc} unreachable: {getattr(e, 'reason', e)}"
    try:
        data = json.loads(body)
    except ValueError:
        return False, f"unexpected response from {parts.netloc}"
    if not data.get("ok"):
        return False, f"Uptime Kuma rejected the push: {data.get('msg')}"
    return True, f"pushed {status} to {parts.netloc}"


def main(argv: list[str] | None = None, cfg=healthcheck_config) -> int:
    parser = argparse.ArgumentParser(description="ytvault health check for Uptime Kuma")
    parser.add_argument("--no-push", action="store_true", help="판정 결과만 출력하고 보내지 않는다")
    parser.add_argument("--json", action="store_true", help="판정에 쓴 값까지 JSON으로 출력한다")
    args = parser.parse_args(argv)

    now = time.time()
    facts = None
    try:
        facts = collect(now, cfg)
        report = evaluate(facts, now, cfg)
    except Exception as e:  # 헬스체커 자신의 오류도 down으로 알린다
        report = Report(UNHEALTHY, "down", f"UNHEALTHY | healthcheck error: {_short(str(e), 120)}")

    print(report.msg)
    if args.json:
        print(json.dumps({"state": report.state, "kuma_status": report.kuma_status,
                          "findings": report.findings, "facts": facts}, ensure_ascii=False, indent=2, default=str))
    if args.no_push:
        return 0
    if not cfg.KUMA_PUSH_URL:
        print("YTVAULT_KUMA_PUSH_URL is not set; nothing was pushed.", file=sys.stderr)
        return 2
    ok, detail = push(cfg.KUMA_PUSH_URL, report.kuma_status, report.msg, report.ping, cfg.PUSH_TIMEOUT)
    if not ok:
        print(f"Push failed: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
