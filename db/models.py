import sqlite3
import time
from contextlib import contextmanager
from config import DB_PATH

STATUS_PENDING = "pending"
STATUS_AWAITING_CHOICE = "awaiting_choice"
STATUS_DOWNLOADING = "downloading"
STATUS_UPLOADING = "uploading"
STATUS_NOTIFYING = "notifying"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

# 나중에 추가된 컬럼. 기존 DB에는 init_db()가 자동으로 붙인다.
#   max_height: 다운로드 해상도 상한(짧은 변 기준). NULL = 아직 안 정함, 0 = 제한 없음
#   choice_deadline: 해상도 선택 마감 시각. 지나면 기본 해상도로 진행
#   stage / progress / stage_detail: 웹에 보여 줄 세부 진행 단계, 진행률(%), 속도 등 부가 정보
_ADDED_COLUMNS = (
    ("max_height", "INTEGER"),
    ("choice_deadline", "REAL"),
    ("stage", "TEXT"),
    ("progress", "REAL"),
    ("stage_detail", "TEXT"),
)
_CLEAR_STAGE = {"stage": None, "progress": None, "stage_detail": None}


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error_msg TEXT,
                file_path TEXT,
                gdrive_link TEXT,
                notion_url TEXT,
                chat_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, sql_type in _ADDED_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
        conn.commit()


@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def create_job(url: str, chat_id: int) -> int:
    now = time.time()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (url, status, chat_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (url, STATUS_PENDING, chat_id, now, now),
        )
        conn.commit()
        return cur.lastrowid


def update_job(job_id: int, **kwargs):
    kwargs["updated_at"] = time.time()
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [job_id]
    with _conn() as conn:
        conn.execute(f"UPDATE jobs SET {fields} WHERE id = ?", values)
        conn.commit()


def requeue_interrupted_jobs() -> int:
    """재시작 등으로 처리 도중 끊긴 job을 다시 대기 상태로 돌린다. 되돌린 개수를 반환."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, error_msg = NULL, stage = NULL, progress = NULL, stage_detail = NULL,"
            " updated_at = ? WHERE status IN (?, ?, ?)",
            (STATUS_PENDING, time.time(), STATUS_DOWNLOADING, STATUS_UPLOADING, STATUS_NOTIFYING),
        )
        conn.commit()
        return cur.rowcount


def set_stage(job_id: int, stage: str | None, progress: float | None = None, detail: str | None = None):
    update_job(job_id, stage=stage, progress=progress, stage_detail=detail)


def request_resolution_choice(job_id: int, deadline: float) -> None:
    update_job(job_id, status=STATUS_AWAITING_CHOICE, choice_deadline=deadline, **_CLEAR_STAGE)


def choose_resolution(job_id: int, max_height: int) -> bool:
    """선택 대기 중인 job에 해상도를 정하고 대기열로 보낸다. 이미 정해졌으면 False."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET max_height = ?, status = ?, choice_deadline = NULL, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (max_height, STATUS_PENDING, time.time(), job_id, STATUS_AWAITING_CHOICE),
        )
        conn.commit()
        return cur.rowcount == 1


def expire_resolution_choices(now: float, default_height: int) -> list[dict]:
    """선택 마감이 지난 job을 기본 해상도로 대기열에 보내고, 보낸 job 목록을 반환."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? AND choice_deadline < ?",
            (STATUS_AWAITING_CHOICE, now),
        ).fetchall()
    return [dict(row) for row in rows if choose_resolution(row["id"], default_height)]


def get_pending_jobs() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
            (STATUS_PENDING,),
        ).fetchall()
        return [dict(r) for r in rows]


def _search_clause(query: str) -> tuple[str, list]:
    """제목/URL 부분 일치 검색. "67" 이나 "#67" 처럼 숫자면 작업 번호도 찾는다."""
    query = query.strip()
    if not query:
        return "", []
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    clause = "title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\'"
    params: list = [like, like]
    number = query.removeprefix("#")
    if number.isdigit():
        clause += " OR id = ?"
        params.append(int(number))
    return f"WHERE ({clause})", params


def count_jobs(query: str = "") -> int:
    where, params = _search_clause(query)
    with _conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM jobs {where}", params).fetchone()[0]


def search_jobs(query: str = "", limit: int = 20, offset: int = 0) -> list[dict]:
    """최근 요청 순으로 검색 결과의 한 페이지를 반환."""
    where, params = _search_clause(query)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [dict(r) for r in rows]


def count_jobs_by_status() -> dict[str, int]:
    with _conn() as conn:
        return dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall())


def get_job(job_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_job_by_url(url: str) -> dict | None:
    """URL 기준으로 가장 최근 job 반환."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE url = ? ORDER BY created_at DESC LIMIT 1",
            (url,),
        ).fetchone()
        return dict(row) if row else None


def retry_failed_job(job_id: int) -> bool:
    """실패한 job만 대기열로 되돌린다. 이미 다른 상태면(중복 클릭 등) 아무것도 하지 않고 False.

    고른 해상도(max_height)는 그대로 두어 다시 묻지 않는다.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, error_msg = NULL, file_path = NULL, gdrive_link = NULL,"
            " notion_url = NULL, stage = NULL, progress = NULL, stage_detail = NULL, updated_at = ?"
            " WHERE id = ? AND status = ?",
            (STATUS_PENDING, time.time(), job_id, STATUS_FAILED),
        )
        conn.commit()
        return cur.rowcount == 1


def reset_job_to_pending(job_id: int) -> None:
    """기존 job을 재처리 대기 상태로 초기화."""
    update_job(
        job_id,
        status=STATUS_PENDING,
        error_msg=None,
        file_path=None,
        gdrive_link=None,
        notion_url=None,
        **_CLEAR_STAGE,
    )
