"""Sync a Notion database into the local Memory Store."""

import os
from typing import Any

import httpx
from dotenv import load_dotenv

from memory_store.store import upsert

load_dotenv()

NOTION_VERSION = "2022-06-28"


def _headers() -> dict[str, str]:
    token = os.getenv("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN is not set in .env")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _plain_text(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items)


def _property_value(prop: dict[str, Any]):
    kind = prop.get("type")
    value = prop.get(kind) if kind else None
    if kind in {"title", "rich_text"}:
        return _plain_text(value or [])
    if kind == "multi_select":
        return [item.get("name", "") for item in value or [] if item.get("name")]
    if kind in {"select", "status"}:
        return (value or {}).get("name")
    if kind == "date":
        return (value or {}).get("start")
    if kind in {"url", "email", "phone_number", "number", "checkbox"}:
        return value
    return None


def _page_title(properties: dict[str, Any]) -> str:
    for prop in properties.values():
        if prop.get("type") == "title":
            return _property_value(prop) or "(untitled)"
    return "(untitled)"


def _block_text(client: httpx.Client, block_id: str) -> str:
    lines: list[str] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        response = client.get(f"https://api.notion.com/v1/blocks/{block_id}/children", params=params)
        response.raise_for_status()
        data = response.json()
        for block in data.get("results", []):
            kind = block.get("type")
            body = block.get(kind, {}) if kind else {}
            text = _plain_text(body.get("rich_text", []))
            if text:
                lines.append(text)
            if block.get("has_children"):
                nested = _block_text(client, block["id"])
                if nested:
                    lines.append(nested)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(lines)


def _infer_domain(title: str, tags: list[str]) -> str:
    text = " ".join([title, *tags]).lower()
    mappings = {
        "health": ("health", "medical", "doctor", "fitness"),
        "work": ("work", "career", "interview", "engineering"),
        "study": ("study", "course", "class", "learning"),
        "ideas": ("idea", "brainstorm"),
        "personal": ("personal", "family", "home", "travel"),
    }
    for domain, keywords in mappings.items():
        if any(keyword in text for keyword in keywords):
            return domain
    return "general"


def sync(limit: int | None = None) -> None:
    database_id = os.getenv("NOTION_DATABASE_ID")
    if not database_id:
        raise RuntimeError("NOTION_DATABASE_ID is not set in .env")

    processed = 0
    cursor = None
    with httpx.Client(headers=_headers(), timeout=30) as client:
        while True:
            payload: dict[str, Any] = {"page_size": min(100, limit - processed) if limit else 100}
            if cursor:
                payload["start_cursor"] = cursor
            response = client.post(f"https://api.notion.com/v1/databases/{database_id}/query", json=payload)
            response.raise_for_status()
            data = response.json()
            for page in data.get("results", []):
                properties = page.get("properties", {})
                normalized = {name: _property_value(prop) for name, prop in properties.items()}
                title = _page_title(properties)
                tags = next((value for value in normalized.values() if isinstance(value, list)), [])
                content = _block_text(client, page["id"])
                upsert({
                    "source_type": "notion",
                    "source_id": page["id"],
                    "title": title,
                    "content": content,
                    "tags": tags,
                    "domain": _infer_domain(title, tags),
                    "source_url": page.get("url", ""),
                    "created_at": page.get("created_time"),
                    "updated_at": page.get("last_edited_time"),
                    "metadata": {"properties": normalized},
                })
                processed += 1
                print(f"  ✓ {title}")
                if limit and processed >= limit:
                    print(f"Synced {processed} Notion page(s).")
                    return
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    print(f"Synced {processed} Notion page(s).")
