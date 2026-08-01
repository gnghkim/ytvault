import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from google import genai

logger = logging.getLogger(__name__)

_client = None

_PROMPT = """다음 유튜브 영상의 텍스트 정보를 분석하여 아래 JSON 형식으로 응답해주세요. 반드시 유효한 JSON만 출력하세요.

{content}

출력 형식:
{{
  "summary": "3~5문장의 핵심 요약 (한국어)",
  "tags": ["태그1", "태그2", "태그3"],
  "category": "Technology/Business/Science/Life/Creative 중 하나"
}}"""


@dataclass
class AIResult:
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = ""


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def _get_model() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _build_content(subtitles: str, description: str) -> str:
    parts = []
    if subtitles:
        parts.append(f"[자막]\n{subtitles[:12000]}")
    if description:
        parts.append(f"[영상 설명]\n{description[:3000]}")
    return "\n\n".join(parts)


async def _summarize_async(subtitles: str, description: str) -> AIResult:
    content = _build_content(subtitles, description)
    if not content:
        logger.warning("No content available for summarization.")
        return AIResult()

    client = _get_client()
    prompt = _PROMPT.format(content=content)
    response = await client.aio.models.generate_content(
        model=_get_model(),
        contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    data = json.loads(response.text)
    return AIResult(
        summary=data.get("summary", ""),
        tags=data.get("tags", []),
        category=data.get("category", ""),
    )


def summarize_youtube(subtitles: str = "", description: str = "") -> AIResult:
    """자막 + 영상 설명으로 요약/태그/카테고리 생성 (동기 래퍼)."""
    try:
        return asyncio.run(_summarize_async(subtitles, description))
    except Exception as e:
        logger.warning(f"Gemini summarization failed: {e}")
        return AIResult()
