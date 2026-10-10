import sqlite3
import time
from contextlib import contextmanager
from config import DB_PATH

STATUS_PENDING = "pending"
STATUS_DOWNLOADING = "downloading"
STATUS_UPLOADING = "uploading"
STATUS_NOTIFYING = "notifying"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


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
            "UPDATE jobs SET status = ?, error_msg = NULL, updated_at = ? WHERE status IN (?, ?, ?)",
            (STATUS_PENDING, time.time(), STATUS_DOWNLOADING, STATUS_UPLOADING, STATUS_NOTIFYING),
        )
        conn.commit()
        return cur.rowcount


def get_pending_jobs() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
            (STATUS_PENDING,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_jobs(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


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


def reset_job_to_pending(job_id: int) -> None:
    """기존 job을 재처리 대기 상태로 초기화."""
    update_job(
        job_id,
        status=STATUS_PENDING,
        error_msg=None,
        file_path=None,
        gdrive_link=None,
        notion_url=None,
    )
