import logging
import os
from notion_client import Client

logger = logging.getLogger(__name__)

_client = None

_TITLE_PROP      = "제목"
_SUMMARY_PROP    = "요약"
_TAGS_PROP       = "태그"
_CATEGORY_PROP   = "카테고리"
_SOURCE_TYPE_PROP = "출처 유형"
_STATUS_PROP     = "상태"
_PRIORITY_PROP   = "중요도"
_ZETTEL_ID_PROP  = "옵시디언 ID"
_ARCHIVE_URL_PROP = "아카이브 링크"


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(auth=os.environ["NOTION_TOKEN"])
    return _client


def _find_existing_page(client: Client, database_id: str, zettel_id: str):
    response = client.databases.query(
        database_id=database_id,
        filter={"property": _ZETTEL_ID_PROP, "rich_text": {"equals": zettel_id}},
    )
    results = response.get("results", [])
    return results[0] if results else None


def save_to_notion(
    database_id: str,
    job_id: int,
    title: str,
    url: str,
    gdrive_link: str,
    summary: str = "",
    tags: list[str] = None,
    category: str = "",
) -> str:
    """Create or update a Notion page. Returns the Notion page URL."""
    client = _get_client()
    zettel_id = f"ytvault-{job_id}"

    properties = {
        _TITLE_PROP:       {"title": [{"text": {"content": title}}]},
        _SUMMARY_PROP:     {"rich_text": [{"text": {"content": summary[:2000]}}] if summary else []},
        _TAGS_PROP:        {"multi_select": [{"name": t} for t in (tags or [])]},
        _CATEGORY_PROP:    {"select": {"name": category or "YouTube"}},
        _SOURCE_TYPE_PROP: {"select": {"name": "YouTube"}},
        _STATUS_PROP:      {"status": {"name": "Inbox"}},
        _PRIORITY_PROP:    {"select": {"name": "1"}},
        _ZETTEL_ID_PROP:   {"rich_text": [{"text": {"content": zettel_id}}]},
        "URL":             {"url": url},
    }
    if gdrive_link:
        properties[_ARCHIVE_URL_PROP] = {"url": gdrive_link}

    existing = _find_existing_page(client, database_id, zettel_id)
    if existing:
        page = client.pages.update(page_id=existing["id"], properties=properties)
        _update_video_block(client, existing["id"], url)
    else:
        page = client.pages.create(
            parent={"database_id": database_id},
            properties=properties,
            children=_video_blocks(url),
        )

    notion_url = page.get("url", "")
    logger.info(f"[notion] saved: {title} → {notion_url}")
    return notion_url


def _video_blocks(url: str) -> list:
    return [
        {
            "object": "block",
            "type": "video",
            "video": {"type": "external", "external": {"url": url}},
        }
    ]


def _update_video_block(client: Client, page_id: str, url: str) -> None:
    """기존 페이지의 video 블록을 교체하거나 없으면 새로 추가."""
    response = client.blocks.children.list(block_id=page_id)
    existing_video = next(
        (b for b in response.get("results", []) if b.get("type") == "video"),
        None,
    )
    if existing_video:
        client.blocks.update(
            block_id=existing_video["id"],
            video={"type": "external", "external": {"url": url}},
        )
    else:
        client.blocks.children.append(
            block_id=page_id,
            children=_video_blocks(url),
        )
