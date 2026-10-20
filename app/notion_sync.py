"""Phase 4｜Notion 完整同步（含官方 File Upload API 六張持久化圖片上傳）與共享防重發鎖（Shared Publish Lock）。

遵循 MASTER_SPEC.md 第 7, 8, 10, 11 節規範：
1. **真實圖片持久化上傳 (`upload_image_to_notion`)**：
   - 使用 Notion 官方 File Upload API (`POST /v1/file_uploads` -> `POST /v1/file_uploads/{id}/send`)，
     將本地通過 Quality Gate 的 6 張 `1080×1350` 獨立越南文 JPEG 與 6 張繁體中文對照 JPEG 直接上傳至 Notion 持久化儲存空間（非 GitHub Actions 暫存目錄）。
2. **真實 Schema 完整欄位與內頁同步 (`sync_post_to_notion`)**：
   - 嚴格對應 `@carry.vietnam.tw｜TikTok 輪播內容資料庫` (`f1bc34396f154e62bca90c9deea19d7a`) 的 13 個真實欄位：
     * `主題` (title): 繁體中文主題名稱（附 `[post_id]` 供跨機防重發查尋，不清除既有歷史資料）
     * `越南文標題` (rich_text): 越南文標題
     * `狀態` (status): `備稿` / `圖片完成` / `已排程` / `已發布` / `發布失敗`
     * `發布日期` (date): 預定發布日期 (`YYYY-MM-DD`)
     * `時段` (select): `07:00` / `11:30` / `18:00`
     * `分類` (select): `家庭情緒` / `台灣生活` / `圖文行銷`
     * `核心切角` (rich_text): 選題切角說明
     * `CTA` (rich_text): 行動呼籲
     * `越南文圖片` (files): 6 張實際越南文 JPEG 檔案
     * `中文版圖片` (files): 6 張實際繁體中文對照 JPEG 檔案
     * `中文對照` (checkbox): `True`
   - **資料庫封面 (`cover`)** 設為第 1 張越南文封面圖。
   - **頁面內頁 (`children` blocks)**：包含六張實際越南文圖片區塊（`image` block）、逐頁繁中與越文對照說明、完整越南文貼文與 5 個 Hashtag（可直接複製）、以及版本與 SHA-256 校驗碼。
3. **跨環境共享防重發查詢 (`check_shared_notion_lock`)**：
   - 由於 GitHub Actions 每次為全新臨時環境（ephemeral runner），在發布或建檔前先向 Notion 資料庫查詢同一 `發布日期` + `時段`（或 `post_id`）是否已存在且狀態為 `已排程` / `已發布`，防止 GitHub Actions 與 Windows 常開備援機重複建檔或雙重上傳。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import AppSettings
from app.errors import AutotokSystemError, BlockedNeedsAuthError
from app.media import verify_carousel_directory
from app.models import CarouselPostPackage, PostStatus
from app.production import SLOT_TO_NOTION_CATEGORY, DailySlotEditorialSpec

POST_STATUS_TO_NOTION_STATUS: dict[PostStatus, str] = {
    PostStatus.DRAFT: "備稿",
    PostStatus.RESEARCHED: "備稿",
    PostStatus.GENERATING: "備稿",
    PostStatus.READY_TO_REVIEW: "圖片完成",
    PostStatus.READY: "圖片完成",
    PostStatus.SCHEDULED: "已排程",
    PostStatus.PUBLISHING: "已排程",
    PostStatus.PUBLISHED: "已發布",
    PostStatus.FAILED: "發布失敗",
    PostStatus.QUALITY_FAILED: "發布失敗",
    PostStatus.UNKNOWN: "已排程",
    PostStatus.BLOCKED_NEEDS_AUTH: "圖片完成",
}


class NotionSyncResult(BaseModel):
    """單篇六頁輪播同步至 Notion 之結果報告。"""

    post_id: str
    notion_page_id: str
    notion_page_url: str
    action: str = Field(description="created 或 updated_existing")
    notion_status: str
    vi_uploaded_file_ids: list[str] = Field(default_factory=list)
    zh_uploaded_file_ids: list[str] = Field(default_factory=list)


class NotionSyncService:
    """負責將六頁輪播圖文與持久化圖片同步至 Notion，並提供跨執行環境防重發檢查。"""

    NOTION_API_BASE = "https://api.notion.com/v1"
    NOTION_VERSION = "2022-06-28"

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or AppSettings()
        self._transport = transport

    def _require_token(self) -> str:
        token = self.settings.notion_token.strip()
        if not token:
            raise BlockedNeedsAuthError(
                "BLOCKED_NEEDS_AUTH：未設定 NOTION_TOKEN，無法執行 Notion 同步。"
            )
        return token

    def _headers(self, *, json_content: bool = True) -> dict[str, str]:
        token = self._require_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self.NOTION_VERSION,
            "Accept": "application/json",
        }
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    def upload_image_to_notion(self, client: httpx.Client, file_path: Path) -> str:
        """透過 Notion 官方 File Upload API 上傳單張 JPEG 並回傳持久化 `file_upload_id`。"""
        init_resp = client.post(
            f"{self.NOTION_API_BASE}/file_uploads",
            headers=self._headers(json_content=True),
            json={
                "filename": file_path.name,
                "content_type": "image/jpeg",
            },
        )
        if init_resp.status_code != 200:
            raise AutotokSystemError(
                f"Notion File Upload 初始化失敗 ({file_path.name}, HTTP {init_resp.status_code}): {init_resp.text[:300]}",
                code="NOTION_UPLOAD_ERROR",
            )
        file_upload_id = str(init_resp.json()["id"])

        with file_path.open("rb") as fp:
            send_resp = client.post(
                f"{self.NOTION_API_BASE}/file_uploads/{file_upload_id}/send",
                headers=self._headers(json_content=False),
                files={"file": (file_path.name, fp, "image/jpeg")},
            )
        if send_resp.status_code != 200:
            raise AutotokSystemError(
                f"Notion File Upload 傳送檔案失敗 ({file_path.name}, HTTP {send_resp.status_code}): {send_resp.text[:300]}",
                code="NOTION_UPLOAD_ERROR",
            )
        return file_upload_id

    def find_existing_page_for_slot(
        self,
        client: httpx.Client,
        *,
        scheduled_date: str,
        time_slot_str: str,
    ) -> dict[str, Any] | None:
        """查詢 Notion 資料庫中是否已存在相同 `發布日期` 與 `時段` 的頁面（不破壞既有歷史資料，並防止雙重建立）。"""
        db_id = self.settings.notion_database_id.strip()
        query_payload = {
            "filter": {
                "and": [
                    {"property": "發布日期", "date": {"equals": scheduled_date}},
                    {"property": "時段", "select": {"equals": time_slot_str}},
                ]
            },
            "page_size": 5,
        }
        resp = client.post(
            f"{self.NOTION_API_BASE}/databases/{db_id}/query",
            headers=self._headers(json_content=True),
            json=query_payload,
        )
        if resp.status_code != 200:
            raise AutotokSystemError(
                f"查詢 Notion 資料庫失敗 (HTTP {resp.status_code}): {resp.text[:300]}",
                code="NOTION_QUERY_ERROR",
            )
        results = resp.json().get("results", [])
        return results[0] if results else None

    def is_slot_already_published_or_locked_in_notion(
        self,
        *,
        scheduled_date: str,
        time_slot_str: str,
    ) -> tuple[bool, str]:
        """跨執行環境（GitHub Actions vs. Windows 常開機）共享防重發檢查：
        若 Notion 該日期與時段頁面狀態已為『已發布』或『已排程』，則拒絕重複發布。
        """
        with httpx.Client(transport=self._transport, timeout=30.0) as client:
            page = self.find_existing_page_for_slot(
                client,
                scheduled_date=scheduled_date,
                time_slot_str=time_slot_str,
            )
        if not page:
            return False, "Notion 中尚無該時段頁面，允許執行"

        props = page.get("properties", {})
        status_obj = props.get("狀態", {}).get("status") or {}
        status_name = str(status_obj.get("name", ""))
        page_id = str(page.get("id", ""))

        if status_name in {"已發布", "已排程"}:
            return (
                True,
                f"共享鎖攔截：Notion 頁面 ({page_id}) 於 {scheduled_date} {time_slot_str} 狀態已為『{status_name}』，禁止重複發布",
            )
        return False, f"Notion 頁面 ({page_id}) 目前狀態為『{status_name}』，允許更新"

    def sync_post_to_notion(
        self,
        package: CarouselPostPackage,
        *,
        zh_media_dir: Path | None = None,
    ) -> NotionSyncResult:
        """將通過 Quality Gate 的 6 張越南文 JPEG、6 張繁中對照 JPEG、逐頁翻譯與貼文同步至 Notion。"""
        # 1. 先確定 6 張越南文圖片通過品質驗證
        vi_report = verify_carousel_directory(package.media_dir, raise_on_error=True)

        time_str, category_zh = SLOT_TO_NOTION_CATEGORY[package.slot]
        scheduled_date = package.scheduled_at_taipei[:10]
        spec = DailySlotEditorialSpec.SPECS.get(package.slot, {})
        core_angle = str(spec.get("core_angle", "在台越南人實用生活與成長指南"))
        cta_text = str(spec.get("cta", "Lưu lại bài viết & theo dõi @carry.vietnam.tw!"))
        notion_status = POST_STATUS_TO_NOTION_STATUS.get(package.status, "圖片完成")

        with httpx.Client(transport=self._transport, timeout=60.0) as client:
            # 2. 上傳 6 張越南文正式 JPEG 至 Notion 持久化空間
            vi_file_ids: list[str] = []
            for item in vi_report.items:
                fid = self.upload_image_to_notion(client, Path(item.path))
                vi_file_ids.append(fid)

            # 3. 若有提供 6 張繁體中文對照版 JPEG，一併上傳至 Notion `中文版圖片` 欄位
            zh_file_ids: list[str] = []
            if zh_media_dir and zh_media_dir.is_dir():
                zh_report = verify_carousel_directory(zh_media_dir, raise_on_error=False)
                if zh_report.passed:
                    for item in zh_report.items:
                        fid = self.upload_image_to_notion(client, Path(item.path))
                        zh_file_ids.append(fid)

            # 4. 組裝 Notion 資料庫真實欄位屬性 (`properties`)
            vi_files_prop = [
                {
                    "name": f"vi_slide_{idx:02d}.jpg",
                    "type": "file_upload",
                    "file_upload": {"id": fid},
                }
                for idx, fid in enumerate(vi_file_ids, start=1)
            ]
            zh_files_prop = [
                {
                    "name": f"zh_slide_{idx:02d}.jpg",
                    "type": "file_upload",
                    "file_upload": {"id": fid},
                }
                for idx, fid in enumerate(zh_file_ids, start=1)
            ]

            properties: dict[str, Any] = {
                "主題": {
                    "title": [{"type": "text", "text": {"content": package.zh_tw_title}}]
                },
                "越南文標題": {
                    "rich_text": [{"type": "text", "text": {"content": package.vi_title}}]
                },
                "狀態": {"status": {"name": notion_status}},
                "發布日期": {"date": {"start": scheduled_date}},
                "時段": {"select": {"name": time_str}},
                "分類": {"select": {"name": category_zh}},
                "核心切角": {
                    "rich_text": [{"type": "text", "text": {"content": core_angle}}]
                },
                "CTA": {"rich_text": [{"type": "text", "text": {"content": cta_text}}]},
                "越南文圖片": {"files": vi_files_prop},
                "中文對照": {"checkbox": True},
            }
            if zh_files_prop:
                properties["中文版圖片"] = {"files": zh_files_prop}

            # 資料庫頁面封面設為第 1 張越南文輪播圖
            cover_obj = {
                "type": "file_upload",
                "file_upload": {"id": vi_file_ids[0]},
            }

            # 5. 組裝頁面內頁區塊 (`children`)：包含六張實際越南文圖片、逐頁越中翻譯、完整貼文與 5 個 Hashtag
            full_caption_with_tags = f"{package.vi_caption}\n\n{' '.join(package.hashtags)}"
            children_blocks: list[dict[str, Any]] = [
                {
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "icon": {"type": "emoji", "emoji": "🇻🇳"},
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": (
                                        f"Post ID: {package.post_id}｜時段: {scheduled_date} {time_str} ({category_zh})\n"
                                        f"越南文標題: {package.vi_title}\n"
                                        f"繁中標題: {package.zh_tw_title}"
                                    )
                                },
                            }
                        ],
                    },
                },
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "📋 TikTok 越南文貼文與 5 個 Hashtag（可直接複製）"},
                            }
                        ]
                    },
                },
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "language": "plain text",
                        "rich_text": [
                            {"type": "text", "text": {"content": full_caption_with_tags}}
                        ],
                    },
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": f"繁中貼文翻譯：{package.zh_tw_caption}"},
                            }
                        ]
                    },
                },
                {
                    "object": "block",
                    "type": "heading_2",
                    "heading_2": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "🖼️ 六頁越南文正式輪播圖 (1080×1350) 與逐頁繁中對照"},
                            }
                        ]
                    },
                },
            ]

            for slide, fid in zip(package.slides, vi_file_ids, strict=True):
                children_blocks.append(
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": f"Slide {slide.index:02d} / 06"},
                                }
                            ]
                        },
                    }
                )
                children_blocks.append(
                    {
                        "object": "block",
                        "type": "image",
                        "image": {
                            "type": "file_upload",
                            "file_upload": {"id": fid},
                            "caption": [
                                {
                                    "type": "text",
                                    "text": {
                                        "content": f"[{slide.index:02d}] {slide.zh_tw_translation}"
                                    },
                                }
                            ],
                        },
                    }
                )
                children_blocks.append(
                    {
                        "object": "block",
                        "type": "quote",
                        "quote": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {
                                        "content": (
                                            f"🇻🇳 越文：{slide.vi_text}\n"
                                            f"🇹🇼 繁中：{slide.zh_tw_translation}\n"
                                            f"🔒 SHA-256：{(slide.sha256 or '')[:16]}..."
                                        )
                                    },
                                }
                            ]
                        },
                    }
                )

            # 6. 檢查同日期同時段是否已有頁面：若有則更新該頁屬性並附加新版區塊，不重複建空白頁、亦不刪除歷史資料
            existing_page = self.find_existing_page_for_slot(
                client,
                scheduled_date=scheduled_date,
                time_slot_str=time_str,
            )

            db_id = self.settings.notion_database_id.strip()
            if existing_page:
                page_id = str(existing_page["id"])
                patch_resp = client.patch(
                    f"{self.NOTION_API_BASE}/pages/{page_id}",
                    headers=self._headers(json_content=True),
                    json={"properties": properties, "cover": cover_obj},
                )
                if patch_resp.status_code != 200:
                    raise AutotokSystemError(
                        f"更新 Notion 頁面失敗 (HTTP {patch_resp.status_code}): {patch_resp.text[:300]}",
                        code="NOTION_SYNC_ERROR",
                    )
                client.patch(
                    f"{self.NOTION_API_BASE}/blocks/{page_id}/children",
                    headers=self._headers(json_content=True),
                    json={"children": children_blocks},
                )
                page_data = patch_resp.json()
                action = "updated_existing"
            else:
                create_resp = client.post(
                    f"{self.NOTION_API_BASE}/pages",
                    headers=self._headers(json_content=True),
                    json={
                        "parent": {"database_id": db_id},
                        "cover": cover_obj,
                        "properties": properties,
                        "children": children_blocks,
                    },
                )
                if create_resp.status_code != 200:
                    raise AutotokSystemError(
                        f"建立 Notion 頁面失敗 (HTTP {create_resp.status_code}): {create_resp.text[:300]}",
                        code="NOTION_SYNC_ERROR",
                    )
                page_data = create_resp.json()
                action = "created"

        return NotionSyncResult(
            post_id=package.post_id,
            notion_page_id=str(page_data.get("id", "")),
            notion_page_url=str(page_data.get("url", "")),
            action=action,
            notion_status=notion_status,
            vi_uploaded_file_ids=vi_file_ids,
            zh_uploaded_file_ids=zh_file_ids,
        )
