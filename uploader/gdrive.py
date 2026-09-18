import os
import time
import logging
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from config import GDRIVE_FOLDER_ID

_ARCHIVE_FOLDER = "YoutubeVault Archive"

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_RETRYABLE_WINERRORS = ("[WinError 10053]", "[WinError 10054]", "[WinError 10060]")
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
_service = None


def _get_token_path() -> str:
    return os.environ.get("GDRIVE_TOKEN_PATH") or "token.json"


def _get_client_secret_path() -> str:
    return (
        os.environ.get("GDRIVE_CLIENT_SECRET_PATH")
        or os.environ.get("GDRIVE_CREDENTIALS_PATH")
        or "client_secret.json"
    )


def _get_service():
    global _service
    if _service is not None:
        return _service

    token_path = _get_token_path()
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_get_client_secret_path(), _SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _reset_service():
    global _service
    _service = None


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, HttpError):
        return getattr(error.resp, "status", None) in _RETRYABLE_HTTP_STATUSES
    msg = str(error)
    if any(t in msg for t in _RETRYABLE_WINERRORS):
        return True
    return any(t in msg.lower() for t in ["connection aborted", "connection reset", "timed out", "tls"])


def _execute(request_factory, retries: int = 3, base_delay: float = 1.0):
    """request_factory()가 만드는 요청을 실행한다.

    execute(num_retries=...)는 resumable 업로드 도중 발생하는 소켓/SSL 에러를
    청크 단위로 재시도한다 (googleapiclient._retry_request가 socket.error를
    잡아준다 — WinError 10053도 여기 해당). 이게 실패하면 바깥 루프에서
    서비스 객체를 새로 만들어 통째로 재시도한다.
    """
    last_err = None
    for attempt in range(retries):
        try:
            return request_factory().execute(num_retries=5)
        except Exception as e:
            last_err = e
            if attempt == retries - 1 or not _is_retryable(e):
                raise
            _reset_service()
            time.sleep(base_delay * (attempt + 1))
    raise last_err


def _find_or_create_folder(service, name: str, parent_id: str = "root") -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name='{escaped}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    result = _execute(lambda: service.files().list(q=query, spaces="drive", fields="files(id)"))
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    folder = _execute(
        lambda: service.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id",
        )
    )
    return folder["id"]


def check_file_exists(gdrive_link: str) -> bool:
    """Drive 링크(파일 또는 폴더)가 실제로 존재하는지 확인."""
    try:
        # /folders/{id} 또는 /d/{id}/view 형식 모두 처리
        if "/folders/" in gdrive_link:
            file_id = gdrive_link.rstrip("/").split("/folders/")[1].split("?")[0]
        else:
            file_id = gdrive_link.rstrip("/").split("/d/")[1].split("/")[0]
        service = _get_service()
        result = _execute(lambda: service.files().get(fileId=file_id, fields="id,trashed"))
        return not result.get("trashed", False)
    except Exception:
        return False


def _sanitize_folder_name(name: str, max_len: int = 50) -> str:
    invalid = r'\/:*?"<>|'
    sanitized = "".join(c if c not in invalid else "_" for c in name)
    return sanitized[:max_len].strip()


def _upload_single_file(service, file_path: str, filename: str, parent_id: str) -> str:
    media = MediaFileUpload(file_path, resumable=True)
    file = _execute(
        lambda: service.files().create(
            body={"name": filename, "parents": [parent_id]},
            media_body=media,
            fields="id",
        )
    )
    return file.get("id")


def upload_video_package(file_path: str, title: str, md_content: str) -> str:
    """영상 + info.md를 날짜_제목 폴더에 업로드하고 폴더 링크 반환.

    구조: YoutubeVault Archive / YYYY-MM / YYYYMMDD_제목 /
              ├── video.mp4
              └── info.md
    """
    import tempfile

    service = _get_service()

    root_id = GDRIVE_FOLDER_ID or "root"
    archive_id = _find_or_create_folder(service, _ARCHIVE_FOLDER, root_id)

    month_label = datetime.now().strftime("%Y-%m")
    month_id = _find_or_create_folder(service, month_label, archive_id)

    date_label = datetime.now().strftime("%Y%m%d")
    folder_name = f"{date_label}_{_sanitize_folder_name(title)}"
    video_folder_id = _find_or_create_folder(service, folder_name, month_id)

    # 영상 파일 업로드
    video_filename = os.path.basename(file_path)
    video_id = _upload_single_file(service, file_path, video_filename, video_folder_id)
    logger.info(f"Uploaded video: {video_filename} ({video_id})")

    # info.md 업로드
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(md_content)
        md_path = f.name
    try:
        md_id = _upload_single_file(service, md_path, "info.md", video_folder_id)
        logger.info(f"Uploaded info.md ({md_id})")
    finally:
        os.unlink(md_path)

    # 폴더 공유 설정
    _execute(
        lambda: service.permissions().create(
            fileId=video_folder_id,
            body={"type": "anyone", "role": "reader"},
        )
    )

    return f"https://drive.google.com/drive/folders/{video_folder_id}"
