"""Notion 資料庫真實 Schema 讀取與唯讀探測介面（Phase 2）。

遵循 MASTER_SPEC.md 第 7 節：
- 既有 Notion 內容資料庫：`f1bc34396f154e62bca90c9deea19d7a`
- Data source ID：`4d575a73-4f98-4ff6-b3f8-da68c40c7125`
- 鐵律：**先讀取真實 schema；不可猜欄位或任意清除既有歷史內容。**
- 當環境未設定 `NOTION_TOKEN` 時，明確回報 `BLOCKED_NEEDS_AUTH`（拋出 `BlockedNeedsAuthError`），絕不偽造已連上 Notion。
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import AppSettings
from app.errors import AutotokSystemError, BlockedNeedsAuthError


class NotionPropertySchema(BaseModel):
    """Notion 資料庫單一欄位之真實 Schema 摘要。"""

    name: str
    prop_id: str
    prop_type: str


class NotionSchemaInspectionReport(BaseModel):
    """Notion 資料庫真實 Schema 探測與邏輯映射報告。"""

    database_id: str
    data_source_id: str
    title: str
    properties: list[NotionPropertySchema] = Field(default_factory=list)
    recommended_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="根據實際讀取到的欄位名稱與型別，建議對應本系統欄位的映射關係（不會猜測不存在的欄位）",
    )
    unmapped_system_fields: list[str] = Field(
        default_factory=list,
        description="既有 Notion 資料庫尚未包含、未來可在不破壞歷史資料下新增的系統欄位",
    )


class NotionInspector:
    """唯讀探測既有 Notion 資料庫真實 Schema，不修改或刪除任何歷史資料。"""

    NOTION_API_BASE = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings()

    def fetch_database_schema(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> NotionSchemaInspectionReport:
        """呼叫 Notion `GET /v1/databases/{database_id}` 讀取真實欄位定義。"""
        token = self.settings.notion_token.strip()
        if not token:
            raise BlockedNeedsAuthError(
                "BLOCKED_NEEDS_AUTH：尚未設定 NOTION_TOKEN，無法讀取 Notion 資料庫真實 Schema "
                f"({self.settings.notion_database_id})。請於 .env 設定 NOTION_TOKEN 後執行 "
                "`uv run python -m app inspect-notion`。"
            )

        db_id = self.settings.notion_database_id.strip()
        url = f"{self.NOTION_API_BASE}/databases/{db_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self.NOTION_VERSION,
            "Accept": "application/json",
        }

        with httpx.Client(transport=transport, timeout=20.0) as client:
            resp = client.get(url, headers=headers)

        if resp.status_code in {401, 403}:
            raise BlockedNeedsAuthError(
                f"BLOCKED_NEEDS_AUTH：Notion API 拒絕存取 (HTTP {resp.status_code})。"
                "請確認 NOTION_TOKEN 有效，且已在該 Notion 資料庫右上角『Connections』中加入您的 Integration。"
            )

        if resp.status_code != 200:
            raise AutotokSystemError(
                f"讀取 Notion 資料庫 Schema 失敗 (HTTP {resp.status_code}): {resp.text[:300]}",
                code="NOTION_API_ERROR",
            )

        data: dict[str, Any] = resp.json()
        return self.parse_schema_payload(
            data,
            database_id=db_id,
            data_source_id=self.settings.notion_data_source_id,
        )

    @staticmethod
    def parse_schema_payload(
        payload: dict[str, Any],
        *,
        database_id: str,
        data_source_id: str,
    ) -> NotionSchemaInspectionReport:
        """從 Notion API 回傳的 JSON 解析真實欄位，並比對本系統邏輯欄位（不臆測未存在欄位）。"""
        title_parts = payload.get("title", [])
        db_title = "".join(part.get("plain_text", "") for part in title_parts) or "Untitled Database"

        raw_props: dict[str, dict[str, Any]] = payload.get("properties", {})
        props: list[NotionPropertySchema] = []
        for name, meta in raw_props.items():
            props.append(
                NotionPropertySchema(
                    name=name,
                    prop_id=str(meta.get("id", "")),
                    prop_type=str(meta.get("type", "unknown")),
                )
            )
        props.sort(key=lambda p: p.name.lower())

        # 僅在真實存在的欄位中尋找相容型別，不強行假設欄位名稱
        system_requirements: dict[str, tuple[set[str], list[str]]] = {
            "post_title": ({"title"}, ["name", "title", "標題", "主題", "tiêu đề"]),
            "status": ({"status", "select"}, ["status", "狀態", "發布狀態", "state"]),
            "scheduled_at": ({"date"}, ["date", "scheduled", "預定時間", "發布時間", "排程時間", "time"]),
            "slot_category": ({"select", "multi_select"}, ["category", "slot", "分類", "時段", "主題分類"]),
            "hashtags": ({"multi_select", "rich_text"}, ["hashtags", "tags", "標籤", "hashtag"]),
            "cover_or_media": ({"files"}, ["files", "media", "圖片", "封面", "輪播圖", "images"]),
            "source_or_post_url": ({"url"}, ["url", "link", "連結", "網址", "tiktok url"]),
        }

        recommended_mapping: dict[str, str] = {}
        unmapped: list[str] = []

        for sys_field, (allowed_types, keywords) in system_requirements.items():
            matched_prop: str | None = None
            for p in props:
                if p.prop_type in allowed_types:
                    lower_name = p.name.lower()
                    if any(kw in lower_name for kw in keywords) or p.prop_type == "title":
                        matched_prop = f"{p.name} ({p.prop_type})"
                        break
            if matched_prop:
                recommended_mapping[sys_field] = matched_prop
            else:
                unmapped.append(sys_field)

        return NotionSchemaInspectionReport(
            database_id=database_id,
            data_source_id=data_source_id,
            title=db_title,
            properties=props,
            recommended_mapping=recommended_mapping,
            unmapped_system_fields=unmapped,
        )
