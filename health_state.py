"""워커와 텔레그램 봇의 활동을 헬스체커(healthcheck.py)에 알리는 상태 파일.

ytvault 프로세스가 기록하고, 별도 프로세스인 헬스체커가 읽는다.
작업이 없으면 DB가 바뀌지 않으므로, 워커가 대기열을 확인한 시각과 봇이 텔레그램에서
업데이트를 받아 온 시각을 여기에 남긴다.

- 임시 파일에 쓰고 os.replace로 바꿔 넣어, 읽는 쪽이 반쯤 쓴 파일을 보지 않는다.
- 평소에는 WRITE_INTERVAL에 한 번만 쓰고, 작업 시작·끝이나 오류처럼 상태가 바뀔 때는 바로 쓴다.
- 기록에 실패해도 다운로드·업로드는 계속한다.
"""

import json
import logging
import os
import tempfile
import threading
import time

import config
from log_redaction import redact_secrets

WRITE_INTERVAL = 15.0
_MAX_ERROR_LENGTH = 200

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {
    "pid": os.getpid(),
    "started_at": time.time(),
    "written_at": None,
    "worker": {
        "last_tick": None,      # 대기열을 마지막으로 확인한 시각
        "current_job": None,    # 처리 중인 job id
        "busy_since": None,
        "loop_errors": 0,       # 연속으로 실패한 대기열 확인 횟수
        "last_error": None,
        "last_error_at": None,
    },
    "bot": {
        "last_ok": None,        # getUpdates가 마지막으로 성공한 시각 (새 메시지가 없어도 성공)
        "consecutive_errors": 0,
        "last_error": None,
        "last_error_kind": None,  # auth / conflict / rate_limit / server / http / network
        "last_error_at": None,
    },
}
_last_write = 0.0
_write_failed = False


def _short_error(error) -> str:
    if isinstance(error, BaseException):
        error = f"{type(error).__name__}: {error}"
    return redact_secrets(error)[:_MAX_ERROR_LENGTH]


def _write_locked() -> None:
    global _last_write, _write_failed
    now = time.time()
    _state["written_at"] = now
    path = config.HEALTH_STATE_PATH
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".health_state.", suffix=".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, path)
        tmp = None
        _last_write = now
        _write_failed = False
    except OSError as e:
        if not _write_failed:  # 같은 경고를 15초마다 남기지 않는다
            logger.warning("Could not write health state %s: %s", path, e)
        _write_failed = True
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _update(section: str, urgent: bool = False, **fields) -> None:
    try:
        with _lock:
            _state[section].update(fields)
            if urgent or time.time() - _last_write >= WRITE_INTERVAL:
                _write_locked()
    except Exception as e:  # 상태 기록 때문에 워커나 봇이 멈추면 안 된다
        logger.warning("Health state update failed: %s", e)


def flush() -> None:
    """지금 상태를 바로 쓴다 (시작할 때)."""
    _update("worker", urgent=True)


def worker_tick() -> None:
    with _lock:
        recovered = _state["worker"]["loop_errors"] > 0
    _update("worker", urgent=recovered, last_tick=time.time(), loop_errors=0)


def worker_job_started(job_id: int) -> None:
    now = time.time()
    _update("worker", urgent=True, current_job=job_id, busy_since=now, last_tick=now)


def worker_job_finished() -> None:
    _update("worker", urgent=True, current_job=None, busy_since=None, last_tick=time.time())


def worker_loop_error(error: BaseException) -> None:
    with _lock:
        count = _state["worker"]["loop_errors"] + 1
    _update("worker", urgent=True, loop_errors=count, last_error=_short_error(error), last_error_at=time.time())


def bot_poll_ok() -> None:
    with _lock:
        recovered = _state["bot"]["consecutive_errors"] > 0
    _update("bot", urgent=recovered, last_ok=time.time(), consecutive_errors=0)


def bot_poll_error(kind: str, error) -> None:
    with _lock:
        count = _state["bot"]["consecutive_errors"] + 1
        # 연속 오류의 첫 번째와 종류가 바뀐 경우만 바로 쓰고, 나머지는 평소 주기를 따른다
        changed = count == 1 or _state["bot"]["last_error_kind"] != kind
    _update(
        "bot",
        urgent=changed,
        consecutive_errors=count,
        last_error=_short_error(error),
        last_error_kind=kind,
        last_error_at=time.time(),
    )
