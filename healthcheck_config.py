"""헬스체커(healthcheck.py) 설정과 판정 기준.

모든 값은 .env나 환경변수로 바꿀 수 있다. 시간은 초, 용량은 GB(2^30 바이트).
기준의 근거와 조정 방법은 docs/HEALTH_MONITORING.md 참고.
"""

import os

import config  # .env를 읽어 둔다


def _number(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


# --- Uptime Kuma Push -------------------------------------------------------
# 모니터의 Push URL. 토큰이 들어 있으므로 .env에만 둔다(소스코드·문서에 적지 않는다)
KUMA_PUSH_URL = os.getenv("YTVAULT_KUMA_PUSH_URL", "").strip()
PUSH_TIMEOUT = _number("HEALTH_PUSH_TIMEOUT", 10)
# DEGRADED(주의)를 Kuma에 up으로 보낼지 down으로 보낼지. 기본 up: 메시지에만 남기고 알림은 보내지 않는다
DEGRADED_STATUS = "down" if os.getenv("HEALTH_DEGRADED_STATUS", "up").strip().lower() == "down" else "up"

# --- 텔레그램 봇 -------------------------------------------------------------
# 정상이면 롱 폴링이 약 10초마다 끝나므로 마지막 성공 시각이 이보다 오래되면 문제다
BOT_POLL_WARN = _number("HEALTH_BOT_POLL_WARN", 120)
BOT_POLL_FAIL = _number("HEALTH_BOT_POLL_FAIL", 300)

# --- 워커 --------------------------------------------------------------------
# 작업이 없을 때 워커는 5초마다 대기열을 확인한다
WORKER_TICK_FAIL = _number("HEALTH_WORKER_TICK_FAIL", 120)
# 대기열 확인이 연속으로 이만큼 실패하면 장애, 1번 이상이면 주의
WORKER_ERRORS_FAIL = _number("HEALTH_WORKER_ERRORS_FAIL", 3)
# 처리 중인 작업의 진행률·단계가 이 시간 동안 바뀌지 않으면 주의 / 장애.
# 전체 소요 시간이 아니라 "마지막 진행 이후 시간"이라 큰 파일이어도 진행 중이면 걸리지 않는다
JOB_STALL_WARN = _number("HEALTH_JOB_STALL_WARN", 10 * 60)
JOB_STALL_FAIL = _number("HEALTH_JOB_STALL_FAIL", 30 * 60)
# 시작 직후 봇·워커가 첫 기록을 남기기 전까지 기다려 주는 시간
STARTUP_GRACE = _number("HEALTH_STARTUP_GRACE", 120)

# --- 작업 큐 -----------------------------------------------------------------
# 워커가 쉬고 있는데 대기 작업이 이만큼 머물러 있으면 처리되지 않는 것 (장애)
QUEUE_STUCK = _number("HEALTH_QUEUE_STUCK", 5 * 60)
# 워커가 다른 작업을 처리 중이라도 대기가 이보다 길면 주의
QUEUE_WAIT_WARN = _number("HEALTH_QUEUE_WAIT_WARN", 2 * 3600)
# 최근 실패를 세는 기간. 이 안에 실패가 있으면 주의
FAILURE_WINDOW = _number("HEALTH_FAILURE_WINDOW", 6 * 3600)
# 마지막 성공 이후 이 기간 안에 연속으로 이만큼 실패하면 장애
FAILURE_STREAK_FAIL = _number("HEALTH_FAILURE_STREAK_FAIL", 3)

# --- 저장장치 ----------------------------------------------------------------
# 병합할 때 최종 파일의 약 2배를 쓴다(4K 30분 영상 7 GB → 13.4 GB)
DISK_FREE_WARN_GB = _number("HEALTH_DISK_FREE_WARN_GB", 20)
DISK_FREE_FAIL_GB = _number("HEALTH_DISK_FREE_FAIL_GB", 5)
DISK_USAGE_WARN_PCT = _number("HEALTH_DISK_USAGE_WARN_PCT", 90)

# --- 웹 대시보드 -------------------------------------------------------------
WEB_TIMEOUT = _number("HEALTH_WEB_TIMEOUT", 5)

# --- 상태를 읽어 올 곳 (ytvault와 같은 값) ----------------------------------
STATE_PATH = config.HEALTH_STATE_PATH
DB_PATH = config.DB_PATH
DOWNLOAD_DIR = config.DOWNLOAD_DIR
DRIVE_TOKEN_PATH = config.project_path(os.getenv("GDRIVE_TOKEN_PATH") or "token.json")
WEB_HOST = config.WEB_HOST
WEB_PORT = config.WEB_PORT
