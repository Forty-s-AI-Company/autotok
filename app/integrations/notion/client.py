"""Notion API 用戶端（httpx，可注入 MockTransport）。只做讀取與「新增欄位」，不刪不改既有資料。"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.errors import AutotokSystemError, BlockedNeedsAuthError
from app.notion_schema import REQUIRED_NOTION_PROPERTIES, STATUS_OPTIONS

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# 新增欄位時送給 Notion 的型別定義
_PROPERTY_BODIES: dict[str, dict[str, Any]] = {
    "rich_text": {"rich_text": {}},
    "select": {"select": {"options": []}},
    "date": {"date": {}},
    "files": {"files": {}},
    "checkbox": {"checkbox": {}},
    "url": {"url": {}},
    "number": {"number": {}},
}


_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def validate_notion_id(value: str) -> str:
    """Notion 的資料庫／頁面 ID 是 32 位 hex（可含連字號）。組進 URL 前先驗證，回傳去掉連字號的小寫 ID。"""
    cleaned = value.strip().replace("-", "")
    if not _ID_RE.match(cleaned):
        raise AutotokSystemError("Notion ID 格式不正確（應為 32 位英數字）。", code="NOTION_BAD_ID")
    return cleaned.lower()


_PARENT_KIND = {"workspace": "workspace", "page_id": "page", "database_id": "database", "block_id": "block"}


def database_create_properties() -> dict[str, Any]:
    """建立資料庫時使用的欄位定義，來源是 `REQUIRED_NOTION_PROPERTIES`。

    Notion API 建立資料庫時無法建立 status 欄位，所以「狀態」改建成 select 並預設選項；
    其餘 select 欄位不預設選項（寫入頁面時 Notion 會自動新增）。
    """
    props: dict[str, Any] = {}
    for name, ptype in REQUIRED_NOTION_PROPERTIES.items():
        if ptype == "title":
            props[name] = {"title": {}}
        elif ptype == "status":
            props[name] = {"select": {"options": [{"name": opt} for opt in STATUS_OPTIONS]}}
        else:
            props[name] = _PROPERTY_BODIES[ptype]
    return props


def plain_title(rich: list[dict[str, Any]] | None) -> str:
    return "".join(str(part.get("plain_text", "")) for part in (rich or []))


class NotionClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self._token = token.strip()
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._token:
            raise BlockedNeedsAuthError("尚未提供 Notion Token。")
        try:
            with httpx.Client(transport=self._transport, timeout=20.0) as client:
                resp = client.request(method, f"{NOTION_API_BASE}{path}", headers=self._headers(), json=json)
        except httpx.HTTPError as exc:
            raise AutotokSystemError(f"連不上 Notion：{exc}", code="NOTION_NETWORK_ERROR") from exc
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 401:
            raise BlockedNeedsAuthError("Notion Token 無效或已被撤銷，請重新連線。")
        if resp.status_code == 403:
            raise BlockedNeedsAuthError("這個 Notion 整合沒有足夠的權限（需要讀取與寫入內容）。")
        if resp.status_code == 404:
            raise AutotokSystemError(
                "找不到這個 Notion 項目，或還沒分享給整合。請在該資料庫右上角「…」→「連線」加入此整合。",
                code="NOTION_NOT_FOUND",
            )
        if resp.status_code == 429:
            raise AutotokSystemError("Notion 請求太頻繁，請稍後再試。", code="NOTION_RATE_LIMITED")
        raise AutotokSystemError(
            f"Notion 回應錯誤（HTTP {resp.status_code}）：{resp.text[:200]}", code="NOTION_API_ERROR"
        )

    def whoami(self) -> dict[str, str]:
        """回傳 `{"name": 整合名稱, "workspace_name": 工作區名稱}`。"""
        data = self._request("GET", "/users/me")
        bot = data.get("bot") or {}
        return {"name": str(data.get("name") or ""), "workspace_name": str(bot.get("workspace_name") or "")}

    def search(self, kind: str = "database", query: str = "", *, limit: int = 50) -> list[dict[str, str]]:
        """列出整合可存取的資料庫（`database`）或頁面（`page`）。"""
        if kind not in {"database", "page"}:
            raise ValueError("kind 只能是 database 或 page")
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": kind},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": min(limit, 100),
        }
        if query:
            body["query"] = query
        data = self._request("POST", "/search", json=body)
        items: list[dict[str, str]] = []
        for r in data.get("results", []):
            if r.get("object") != kind:
                continue
            if kind == "database":
                title = plain_title(r.get("title"))
            else:
                title = ""
                for prop in (r.get("properties") or {}).values():
                    if prop.get("type") == "title":
                        title = plain_title(prop.get("title"))
                        break
            parent = r.get("parent") or {}
            parent_type = _PARENT_KIND.get(str(parent.get("type", "")), "")
            parent_id = str(parent.get(str(parent.get("type", ""))) or "") if parent_type in {"page", "database"} else ""
            items.append(
                {
                    "id": str(r.get("id", "")),
                    "kind": kind,
                    "title": title or "（無標題）",
                    "url": str(r.get("url", "")),
                    "last_edited_time": str(r.get("last_edited_time", "")),
                    "parent_type": parent_type,
                    "parent_id": parent_id,
                    "parent_title": "",
                }
            )
        # 上層是搜尋結果裡的頁面或資料庫時才補上標題，取不到就留空，不硬湊
        titles = {it["id"].replace("-", ""): it["title"] for it in items}
        for it in items:
            if it["parent_id"]:
                it["parent_title"] = titles.get(it["parent_id"].replace("-", ""), "")
        return items

    def get_database(self, database_id: str) -> dict[str, Any]:
        return self._request("GET", f"/databases/{validate_notion_id(database_id)}")

    def add_properties(self, database_id: str, props: dict[str, str]) -> dict[str, Any]:
        """只新增欄位（名稱 → 型別）。title、status 型別 Notion API 不支援新增，會直接拒絕。"""
        validate_notion_id(database_id)
        body: dict[str, Any] = {}
        for name, ptype in props.items():
            if ptype not in _PROPERTY_BODIES:
                raise AutotokSystemError(
                    f"「{name}」是 {ptype} 欄位，Notion API 無法自動新增，請在 Notion 內手動建立。",
                    code="NOTION_PROPERTY_NOT_CREATABLE",
                )
            body[name] = _PROPERTY_BODIES[ptype]
        if not body:
            return {}
        return self._request("PATCH", f"/databases/{validate_notion_id(database_id)}", json={"properties": body})

    def create_page(self, parent_page_id: str, title: str) -> dict[str, str]:
        """在指定頁面底下新增一個空白頁面。回傳 `{"id", "url"}`。"""
        data = self._request(
            "POST",
            "/pages",
            json={
                "parent": {"type": "page_id", "page_id": validate_notion_id(parent_page_id)},
                "properties": {"title": {"title": [{"type": "text", "text": {"content": title}}]}},
            },
        )
        return {"id": str(data.get("id", "")), "url": str(data.get("url", ""))}

    def create_database(self, parent_page_id: str, title: str) -> dict[str, str]:
        """在指定頁面底下新增資料庫（欄位見 `database_create_properties`）。回傳 `{"id", "url"}`。"""
        data = self._request(
            "POST",
            "/databases",
            json={
                "parent": {"type": "page_id", "page_id": validate_notion_id(parent_page_id)},
                "title": [{"type": "text", "text": {"content": title}}],
                "properties": database_create_properties(),
            },
        )
        return {"id": str(data.get("id", "")), "url": str(data.get("url", ""))}
