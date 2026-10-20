"""Notion 同步的安全行為：不誤寫他人品牌、長文切段、先附加再刪舊、分頁刪除、共用資料庫拒絕。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.config import AppSettings
from app.errors import AutotokSystemError
from app.integrations.notion import oauth
from app.integrations.notion.binding import find_database_owner
from app.integrations.notion.client import validate_notion_id
from app.models import ContentSlot, TikTokAccountProfile
from app.notion_sync import NotionSyncService, rich_text
from app.production import produce_carousel_with_qa_retry
from app.state import PostStateRepository
from app.web.main import create_app


@pytest.fixture(autouse=True)
def memory_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "set_secret", lambda ref, value, **_: store.__setitem__(ref, value))
    monkeypatch.setattr(credentials, "get_secret", lambda ref, **_: store.get(ref, ""))
    monkeypatch.setattr(credentials, "delete_secret", lambda ref, **_: store.pop(ref, None))


def _package(tmp_path: Path, **update: Any):  # noqa: ANN202
    pkg, _, zh_dir = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130, post_id="p-safe", scheduled_date_str="2026-10-25", output_base_dir=tmp_path
    )
    return pkg.model_copy(update=update), zh_dir


class Recorder:
    """假 Notion：記錄所有請求，可設定既有頁面、舊區塊與失敗行為。"""

    def __init__(self, *, existing: bool = False, old_blocks: int = 0, append_status: int = 200, page_size: int = 100) -> None:
        self.existing, self.old_blocks, self.append_status, self.page_size = existing, old_blocks, append_status, page_size
        self.calls: list[tuple[str, str]] = []
        self.create_payload: dict[str, Any] = {}
        self.patch_page_payload: dict[str, Any] = {}
        self.append_payloads: list[dict[str, Any]] = []
        self.uploads = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        self.calls.append((method, request.url.path))
        if url.endswith("/v1/file_uploads"):
            self.uploads += 1
            return httpx.Response(200, json={"id": f"f{self.uploads}"})
        if "/send" in url:
            return httpx.Response(200, json={"status": "uploaded"})
        if url.endswith("/query"):
            res = [{"id": "page-1", "properties": {"狀態": {"status": {"name": "圖片完成"}}}}] if self.existing else []
            return httpx.Response(200, json={"results": res})
        if method == "POST" and url.endswith("/v1/pages"):
            self.create_payload = json.loads(request.content)
            return httpx.Response(200, json={"id": "new-page", "url": "https://notion.so/new-page"})
        if method == "PATCH" and request.url.path == "/v1/pages/page-1":
            self.patch_page_payload = json.loads(request.content)
            return httpx.Response(200, json={"id": "page-1", "url": "https://notion.so/page-1"})
        if method == "GET" and request.url.path == "/v1/blocks/page-1/children":
            start = int(request.url.params.get("start_cursor", "0"))
            end = min(start + self.page_size, self.old_blocks)
            return httpx.Response(
                200,
                json={
                    "results": [{"id": f"old-{i}"} for i in range(start, end)],
                    "has_more": end < self.old_blocks,
                    "next_cursor": str(end) if end < self.old_blocks else None,
                },
            )
        if method == "PATCH" and request.url.path == "/v1/blocks/page-1/children":
            self.append_payloads.append(json.loads(request.content))
            return httpx.Response(self.append_status, json={})
        if method == "DELETE" and request.url.path.startswith("/v1/blocks/"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    @property
    def deletes(self) -> list[int]:
        return [i for i, (m, _) in enumerate(self.calls) if m == "DELETE"]

    @property
    def appends(self) -> list[int]:
        return [i for i, (m, p) in enumerate(self.calls) if m == "PATCH" and p.endswith("/children")]


def _service(rec: Recorder) -> NotionSyncService:
    return NotionSyncService(AppSettings(NOTION_TOKEN="t", NOTION_DATABASE_ID="d" * 32), transport=httpx.MockTransport(rec))


def test_rich_text_chunks_and_empty() -> None:
    assert rich_text("") == []
    chunks = rich_text("a" * 4500)
    assert [len(c["text"]["content"]) for c in chunks] == [2000, 2000, 500]


def test_empty_cta_and_angle_do_not_fall_back_to_default_brand(tmp_path: Path) -> None:
    pkg, zh = _package(tmp_path, cta="", core_angle="", account_id="other-acct")
    rec = Recorder()
    _service(rec).sync_post_to_notion(pkg, zh_media_dir=zh, language_label="泰文")
    props = rec.create_payload["properties"]
    assert props["CTA"]["rich_text"] == [] and props["核心切角"]["rich_text"] == []
    blob = json.dumps(rec.create_payload["children"], ensure_ascii=False)
    assert "越南文" not in blob and "六頁" not in blob and "🇻🇳" not in blob
    assert "泰文" in blob


def test_long_caption_is_split_into_multiple_rich_text_items(tmp_path: Path) -> None:
    pkg, zh = _package(tmp_path, vi_caption="b" * 3900)
    rec = Recorder()
    _service(rec).sync_post_to_notion(pkg, zh_media_dir=zh)
    code = next(b for b in rec.create_payload["children"] if b["type"] == "code")
    items = code["code"]["rich_text"]
    assert len(items) >= 2 and all(len(i["text"]["content"]) <= 2000 for i in items)
    assert "".join(i["text"]["content"] for i in items).startswith("b" * 3900)


def test_append_failure_keeps_old_blocks(tmp_path: Path) -> None:
    pkg, zh = _package(tmp_path)
    rec = Recorder(existing=True, old_blocks=5, append_status=500)
    with pytest.raises(AutotokSystemError, match="舊的內頁內容沒有被刪除"):
        _service(rec).sync_post_to_notion(pkg, zh_media_dir=zh)
    assert rec.deletes == []


def test_old_blocks_are_paginated_and_deleted_after_append(tmp_path: Path) -> None:
    pkg, zh = _package(tmp_path)
    rec = Recorder(existing=True, old_blocks=250, page_size=100)
    result = _service(rec).sync_post_to_notion(pkg, zh_media_dir=zh)
    assert result.action == "updated_existing"
    assert len(rec.deletes) == 250
    assert rec.appends and max(rec.appends) < min(rec.deletes)  # 先附加、後刪除
    assert all(len(p["children"]) <= 100 for p in rec.append_payloads)


def test_delete_failure_is_reported(tmp_path: Path) -> None:
    pkg, zh = _package(tmp_path)
    rec = Recorder(existing=True, old_blocks=3)
    base = rec.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(400, json={})
        return base(request)

    svc = NotionSyncService(AppSettings(NOTION_TOKEN="t", NOTION_DATABASE_ID="d" * 32), transport=httpx.MockTransport(handler))
    with pytest.raises(AutotokSystemError, match="刪除失敗"):
        svc.sync_post_to_notion(pkg, zh_media_dir=zh)


def test_database_cannot_be_shared_between_accounts(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "s.db")
    repo.upsert_account(TikTokAccountProfile(account_id="second", handle="@second", display_name="Second", notion_database_id=""))
    repo.save_notion_binding("carry-vietnam-tw", database_id="A" * 32)
    cfg = AppSettings(NOTION_TOKEN="", SQLITE_DB_PATH=tmp_path / "s.db")
    assert find_database_owner(repo, cfg, "a" * 32, "second") == "@carry.vietnam.tw"
    assert find_database_owner(repo, cfg, "a" * 32, "carry-vietnam-tw") is None
    assert find_database_owner(repo, cfg, "b" * 32, "second") is None


def test_bind_database_route_rejects_shared_and_bad_ids(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/users/me":
            return httpx.Response(200, json={"bot": {"workspace_name": "WS"}})
        return httpx.Response(500)

    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "w.db", NOTION_TOKEN=""), allowed_hosts={"testserver"})
    app.state.notion_transport = httpx.MockTransport(handler)
    repo = app.state.repo
    repo.upsert_account(TikTokAccountProfile(account_id="second", handle="@second", display_name="Second", notion_database_id=""))
    repo.save_notion_binding("carry-vietnam-tw", database_id="c" * 32)
    credentials.set_secret("notion:second", "tok")
    repo.save_notion_binding("second", token_ref="notion:second")
    c = TestClient(app)
    hdr = {"X-AutoTok-Token": app.state.csrf_token}

    shared = c.post("/notion/bind-database", data={"account_id": "second", "database_id": "C" * 32}, headers=hdr)
    assert "@carry.vietnam.tw" in shared.headers["HX-Trigger"].encode().decode("unicode_escape")
    assert repo.get_notion_binding("second")["database_id"] == ""
    bad = c.post("/notion/bind-database", data={"account_id": "second", "database_id": "../../users"}, headers=hdr)
    assert "格式不正確" in bad.headers["HX-Trigger"].encode().decode("unicode_escape")
    bad_parent = c.post("/notion/bind-parent", data={"account_id": "second", "page_id": "x/y"}, headers=hdr)
    assert "格式不正確" in bad_parent.headers["HX-Trigger"].encode().decode("unicode_escape")


def test_validate_notion_id() -> None:
    assert validate_notion_id("12345678-1234-1234-1234-1234567890AB") == "12345678123412341234" "1234567890ab"
    for bad in ("", "db1", "../x", "g" * 32, "1" * 31):
        with pytest.raises(AutotokSystemError):
            validate_notion_id(bad)


def test_oauth_pending_states_are_capped() -> None:
    first = oauth.new_state("a")
    for _ in range(oauth.MAX_PENDING_STATES + 5):
        oauth.new_state("a")
    assert oauth.pop_state(first) is None  # 最舊的被丟掉
    assert len(oauth._states) <= oauth.MAX_PENDING_STATES
