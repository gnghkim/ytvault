import logging
import os
import re
from urllib.parse import quote


_REDACTED = "[REDACTED]"
_SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "GEMINI_API_KEY",
    "NOTION_TOKEN",
)
_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])\d{6,12}:[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(r"(?:secret|ntn)_[A-Za-z0-9_-]{20,}", re.IGNORECASE),
    re.compile(r"ya29\.[A-Za-z0-9._-]+"),
    re.compile(r"GOCSPX-[A-Za-z0-9_-]+"),
)
_AUTH_HEADER_PATTERN = re.compile(
    r"((?:authorization|x-goog-api-key)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s,\"'}]+",
    re.IGNORECASE,
)


def redact_secrets(value: object) -> str:
    """Remove configured credentials and common credential shapes from text."""
    text = str(value)
    for env_var in _SECRET_ENV_VARS:
        secret = os.getenv(env_var, "")
        if len(secret) < 8:
            continue
        text = text.replace(secret, _REDACTED)
        encoded = quote(secret, safe="")
        if encoded != secret:
            text = text.replace(encoded, _REDACTED)

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return _AUTH_HEADER_PATTERN.sub(rf"\1{_REDACTED}", text)


class RedactingFormatter(logging.Formatter):
    """Redact secrets after messages, arguments, and tracebacks are formatted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_safe_logging(
    level: int = logging.INFO,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(fmt))
    logging.basicConfig(level=level, handlers=[handler], force=True)

    # Routine HTTP request logs can expose sensitive URLs and add little value.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
