"""HTTP Basic authentication for the web UI.

The UI lists every archived video with its Drive and Notion links and binds to
all interfaces by default, so it must not be readable without credentials.
"""

import asyncio
import base64
import binascii
import hmac
import logging

from fastapi import HTTPException, Request, status

import config

logger = logging.getLogger(__name__)

REALM = "YTVault"
FAILURE_DELAY_SECONDS = 1.0


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    """Decode an ``Authorization: Basic`` header.

    Parsed here rather than with FastAPI's HTTPBasic, which accepts ASCII only:
    a non-ASCII password would be rejected before the checks below ran. Browsers
    send UTF-8 when the challenge carries ``charset="UTF-8"``.
    """
    if not header:
        return None
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        raw = base64.b64decode(param.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    username, separator, password = text.partition(":")
    return (username, password) if separator else None


def _matches(supplied: str, expected: str) -> bool:
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


async def require_auth(request: Request) -> None:
    # Read at request time so tests (and a reloaded config) see the current values.
    password = config.WEB_PASSWORD
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_PASSWORD is not set; the web UI is disabled.",
        )
    header = request.headers.get("Authorization")
    credentials = _parse_basic(header)
    if credentials is not None:
        # Evaluate both comparisons so the response time does not reveal which one failed.
        user_ok = _matches(credentials[0], config.WEB_USERNAME)
        password_ok = _matches(credentials[1], password)
        if user_ok and password_ok:
            return
    if header:
        client = request.client.host if request.client else "unknown"
        logger.warning("Web UI login failed from %s", client)
        await asyncio.sleep(FAILURE_DELAY_SECONDS)  # slows down guessing
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"'},
    )
