"""Phase 4｜Notion 六張持久化圖片同步、跨環境共享防重發鎖與本機 Web GUI 儀表板測試套件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import AppSettings
from app.errors import BlockedNeedsAuthError
from app.gui import (
    _run_research_and_analytics_job,
    get_task_state_snapshot,
    render_dashboard_html,
)
from app.models import ContentSlot
from app.notion_sync import NotionSyncService
from app.production import produce_carousel_with_qa_retry


def test_notion_sync_service_uploads_12_images_and_creates_page(tmp_path: Path) -> None:
    """驗證 NotionSyncService 能透過 File Upload API 上傳 6 張越文圖 + 6 張中文圖並建立包含 6 個 image blocks 的頁面。"""
    pkg, _, zh_dir = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="p4-sync-001",
        scheduled_date_str="2026-10-25",
        output_base_dir=tmp_path,
    )

    upload_counter = 0
    captured_create_payload: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_counter, captured_create_payload
        url = str(request.url)

        if url.endswith("/v1/file_uploads") and request.method == "POST":
            upload_counter += 1
            return httpx.Response(200, json={"id": f"fid-{upload_counter:02d}", "status": "pending"})

        if "/send" in url and request.method == "POST":
            return httpx.Response(200, json={"status": "uploaded"})

        if url.endswith("/query") and request.method == "POST":
            return httpx.Response(200, json={"results": []})

        if url.endswith("/v1/pages") and request.method == "POST":
            import json

            captured_create_payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "page-uuid-1234",
                    "url": "https://www.notion.so/page-uuid-1234",
                },
            )

        return httpx.Response(404)

    settings = AppSettings(
        NOTION_TOKEN="ntn_mock_token_123",
        NOTION_DATABASE_ID="f1bc34396f154e62bca90c9deea19d7a",
    )
    service = NotionSyncService(settings=settings, transport=httpx.MockTransport(handler))
    res = service.sync_post_to_notion(pkg, zh_media_dir=zh_dir)

    assert res.action == "created"
    assert res.notion_page_id == "page-uuid-1234"
    assert len(res.vi_uploaded_file_ids) == 6
    assert len(res.zh_uploaded_file_ids) == 6

    props = captured_create_payload["properties"]
    assert props["狀態"]["status"]["name"] == "圖片完成"
    assert props["時段"]["select"]["name"] == "11:30"
    assert props["分類"]["select"]["name"] == "台灣生活"
    assert len(props["越南文圖片"]["files"]) == 6
    assert len(props["中文版圖片"]["files"]) == 6

    children = captured_create_payload["children"]
    image_blocks = [b for b in children if b.get("type") == "image"]
    assert len(image_blocks) == 6


def test_shared_notion_lock_prevents_duplicate_publish() -> None:
    """驗證當 Notion 中該日期與時段狀態已為『已發布』或『已排程』時，共享鎖會攔截重複執行。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "existing-page-99",
                        "properties": {
                            "狀態": {"status": {"name": "已發布"}},
                        },
                    }
                ]
            },
        )

    settings = AppSettings(NOTION_TOKEN="ntn_mock_token_123")
    service = NotionSyncService(settings=settings, transport=httpx.MockTransport(handler))
    locked, reason = service.is_slot_already_published_or_locked_in_notion(
        scheduled_date="2026-10-21",
        time_slot_str="07:00",
    )
    assert locked is True
    assert "共享鎖攔截" in reason


def test_notion_sync_requires_token() -> None:
    """驗證未設定 NOTION_TOKEN 時拋出 BlockedNeedsAuthError。"""
    service = NotionSyncService(settings=AppSettings(NOTION_TOKEN=""))
    with pytest.raises(BlockedNeedsAuthError):
        service.is_slot_already_published_or_locked_in_notion(
            scheduled_date="2026-10-21", time_slot_str="07:00"
        )


def test_gui_dashboard_html_renders_without_leaking_secrets(tmp_path: Path) -> None:
    """驗證本機 Web GUI 儀表板能正常渲染並展示已生產之輪播圖，且不洩露敏感憑證。"""
    prod_base = tmp_path / "artifacts" / "daily_production" / "2026-10-21"
    produce_carousel_with_qa_retry(
        slot=ContentSlot.MORNING_0700,
        post_id="post-2026-10-21-0700",
        scheduled_date_str="2026-10-21",
        output_base_dir=prod_base,
    )

    settings = AppSettings(
        SQLITE_DB_PATH=tmp_path / "gui.db",
        NOTION_TOKEN="ntn_super_secret_should_not_appear_in_html",
    )
    html_out = render_dashboard_html(tmp_path, settings)

    assert "Carry | Đài Loan × Việt Nam" in html_out
    assert "post-2026-10-21-0700" in html_out
    assert "核心切角" in html_out
    assert "CTA 行動呼籲" in html_out
    assert "ntn_super_secret_should_not_appear_in_html" not in html_out

    # 測試一鍵更新市場研究與成效分析背景函式不會拋出參數錯誤
    _run_research_and_analytics_job(tmp_path, settings)
    snap = get_task_state_snapshot()
    assert snap["status"] == "completed"
    assert "✅ 已成功更新" in str(snap["message"])
