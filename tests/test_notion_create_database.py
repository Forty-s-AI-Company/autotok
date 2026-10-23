"""自動建立頁面與資料庫、select 型「狀態」、既有 status 型不受影響（全部使用 MockTransport）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.config import AppSettings
from app.integrations.notion import binding as nb
from app.integrations.notion.client import NotionClient, database_create_properties
from app.integrations.notion.schema_check import check_schema
from app.models import ContentSlot
from app.notion_schema import REQUIRED_NOTION_PROPERTIES, STATUS_OPTIONS
from app.notion_sync import POST_STATUS_TO_NOTION_STATUS, NotionSyncService
from app.production import produce_carousel_with_qa_retry
from app.web.main import create_app

ACCOUNT = "carry-vietnam-tw"
PARENT = "a" * 32
NEW_PAGE = "b" * 32
NEW_DB = "c" * 32
DB_URL = "https://www.notion.so/" + NEW_DB
PAGE_URL = "https://www.notion.so/" + NEW_PAGE


@pytest.fixture(autouse=True)
def memory_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "set_secret", lambda ref, value, **_: store.__setitem__(ref, value))
    monkeypatch.setattr(credentials, "get_secret", lambda ref, **_: store.get(ref, ""))
    monkeypatch.setattr(credentials, "delete_secret", lambda ref, **_: store.pop(ref, None))


class FakeNotion:
    """記錄請求的假 Notion。`db_fail` 讓建立資料庫回 400。"""

    def __init__(self, *, db_fail: bool = False, page_fail: bool = False) -> None:
        self.db_fail, self.page_fail = db_fail, page_fail
        self.calls: list[tuple[str, str]] = []
        self.page_bodies: list[dict[str, Any]] = []
        self.db_bodies: list[dict[str, Any]] = []
        self.created_props: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if path == "/v1/users/me":
            return httpx.Response(200, json={"bot": {"workspace_name": "WS"}})
        if path == "/v1/search":
            kind = body["filter"]["value"]
            if kind == "page":
                return httpx.Response(200, json={"results": [
                    {"object": "page", "id": PARENT, "url": "u", "last_edited_time": "2026-10-20T01:00:00Z", "parent": {"type": "workspace", "workspace": True},
                     "properties": {"title": {"type": "title", "title": [{"plain_text": "工作筆記"}]}}},
                    {"object": "page", "id": "d" * 32, "url": "u", "last_edited_time": "2026-10-20T01:00:00Z", "parent": {"type": "page_id", "page_id": PARENT},
                     "properties": {"title": {"type": "title", "title": [{"plain_text": "子頁面"}]}}},
                ]})
            return httpx.Response(200, json={"results": []})
        if method == "POST" and path == "/v1/pages":
            self.page_bodies.append(body)
            if self.page_fail:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"id": NEW_PAGE, "url": PAGE_URL})
        if method == "POST" and path == "/v1/databases":
            self.db_bodies.append(body)
            if self.db_fail:
                return httpx.Response(400, json={"message": "bad"})
            self.created_props = body["properties"]
            return httpx.Response(200, json={"id": NEW_DB, "url": DB_URL})
        if method == "GET" and path == f"/v1/databases/{NEW_DB}":
            props = {name: {"id": name, "type": next(iter(spec))} for name, spec in self.created_props.items()}
            return httpx.Response(200, json={"id": NEW_DB, "title": [{"plain_text": "AutoTok"}], "properties": props})
        return httpx.Response(500)


def _web(tmp_path: Path, fake: FakeNotion) -> tuple[TestClient, Any, dict[str, str]]:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "w.db", NOTION_TOKEN=""), allowed_hosts={"testserver"})
    app.state.notion_transport = httpx.MockTransport(fake)
    nb.store_connection(app.state.repo, ACCOUNT, token="tok", auth_method="token", workspace_name="WS")
    return TestClient(app), app, {"X-AutoTok-Token": app.state.csrf_token}


def test_create_properties_come_from_required_list_and_use_select_status() -> None:
    props = database_create_properties()
    assert set(props) == set(REQUIRED_NOTION_PROPERTIES)
    assert props["狀態"] == {"select": {"options": [{"name": n} for n in STATUS_OPTIONS]}}
    assert props["時段"] == {"select": {"options": []}} and props["主題"] == {"title": {}}
    assert not any("status" in spec for spec in props.values())
    assert set(POST_STATUS_TO_NOTION_STATUS.values()) <= set(STATUS_OPTIONS)


def test_client_create_page_and_database_payloads() -> None:
    fake = FakeNotion()
    client = NotionClient("tok", transport=httpx.MockTransport(fake))
    assert client.create_page(PARENT, "名稱") == {"id": NEW_PAGE, "url": PAGE_URL}
    assert fake.page_bodies[0]["parent"] == {"type": "page_id", "page_id": PARENT}
    assert fake.page_bodies[0]["properties"]["title"]["title"][0]["text"]["content"] == "名稱"
    client.create_database(NEW_PAGE, "名稱")
    assert fake.db_bodies[0]["parent"]["page_id"] == NEW_PAGE


def test_search_returns_parent_info_only_when_known() -> None:
    items = NotionClient("tok", transport=httpx.MockTransport(FakeNotion())).search("page")
    assert items[0]["parent_type"] == "workspace" and items[0]["parent_title"] == ""
    assert items[1]["parent_type"] == "page" and items[1]["parent_title"] == "工作筆記"


def test_schema_check_accepts_select_status_but_not_other_types() -> None:
    props = {n: {"type": t} for n, t in REQUIRED_NOTION_PROPERTIES.items()}
    props["狀態"] = {"type": "select"}
    assert check_schema(props).ok
    props["狀態"] = {"type": "rich_text"}
    assert check_schema(props).type_mismatch[0].name == "狀態"


def test_create_flow_creates_page_then_database_and_binds(tmp_path: Path) -> None:
    fake = FakeNotion()
    client, app, hdr = _web(tmp_path, fake)
    r = client.post("/notion/create-database", data={"account_id": ACCOUNT, "parent_page_id": PARENT, "name": "AutoTok｜測試"}, headers=hdr)
    assert r.status_code == 200 and "notionChanged" in r.headers["HX-Trigger"]
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] in {"/v1/pages", "/v1/databases"}]
    assert posts == [("POST", "/v1/pages"), ("POST", "/v1/databases")]  # 先頁面、後資料庫
    assert fake.db_bodies[0]["parent"]["page_id"] == NEW_PAGE  # 資料庫放在新頁面內，不是所選頁面
    b = app.state.repo.get_notion_binding(ACCOUNT)
    assert b["database_id"] == NEW_DB and b["database_title"] == "AutoTok｜測試" and b["parent_page_id"] == PARENT
    assert b["schema_missing"] == [] and b["verified_at"] and b["schema_checked_at"]
    # 不碰任何既有項目：沒有 PATCH / DELETE
    assert not [c for c in fake.calls if c[0] in {"PATCH", "DELETE"}]
    panel = client.get(f"/notion/panel?account_id={ACCOUNT}").text
    assert "已建立並綁定" in panel and DB_URL in panel and "全部相符" in panel
    assert "已建立並綁定" not in client.get(f"/notion/panel?account_id={ACCOUNT}").text  # 訊息只顯示一次


def test_database_failure_reports_empty_page_and_retry_reuses_it(tmp_path: Path) -> None:
    fake = FakeNotion(db_fail=True)
    client, app, hdr = _web(tmp_path, fake)
    data = {"account_id": ACCOUNT, "parent_page_id": PARENT, "name": "AutoTok｜測試"}
    r = client.post("/notion/create-database", data=data, headers=hdr)
    trigger = json.loads(r.headers["HX-Trigger"])["toast"]
    assert trigger["kind"] == "err" and PAGE_URL in trigger["msg"] and "不會重複建立" in trigger["msg"]
    assert app.state.repo.get_notion_binding(ACCOUNT)["database_id"] == ""
    assert "建立到一半的頁面" in client.get(f"/notion/panel?account_id={ACCOUNT}").text

    fake.db_fail = False
    client.post("/notion/create-database", data=data, headers=hdr)
    assert len(fake.page_bodies) == 1  # 沒有再建一個空頁面
    assert app.state.repo.get_notion_binding(ACCOUNT)["database_id"] == NEW_DB
    assert "建立到一半的頁面" not in client.get(f"/notion/panel?account_id={ACCOUNT}").text


def test_page_failure_creates_nothing_else(tmp_path: Path) -> None:
    fake = FakeNotion(page_fail=True)
    client, app, hdr = _web(tmp_path, fake)
    r = client.post("/notion/create-database", data={"account_id": ACCOUNT, "parent_page_id": PARENT}, headers=hdr)
    assert json.loads(r.headers["HX-Trigger"])["toast"]["kind"] == "err"
    assert fake.db_bodies == []
    assert app.state.repo.get_notion_binding(ACCOUNT)["database_id"] == ""


def test_already_bound_refuses_unless_replace_confirmed(tmp_path: Path) -> None:
    fake = FakeNotion()
    client, app, hdr = _web(tmp_path, fake)
    app.state.repo.save_notion_binding(ACCOUNT, database_id="e" * 32, database_title="舊的")
    r = client.post("/notion/create-database", data={"account_id": ACCOUNT, "parent_page_id": PARENT}, headers=hdr)
    assert "不會重複建立" in json.loads(r.headers["HX-Trigger"])["toast"]["msg"]
    assert fake.page_bodies == [] and fake.db_bodies == []
    client.post("/notion/create-database", data={"account_id": ACCOUNT, "parent_page_id": PARENT, "replace": "1"}, headers=hdr)
    assert app.state.repo.get_notion_binding(ACCOUNT)["database_id"] == NEW_DB
    assert len(fake.db_bodies) == 1


def test_parent_page_search_and_panel_copy(tmp_path: Path) -> None:
    client, _, _ = _web(tmp_path, FakeNotion())
    html = client.get(f"/notion/parent-pages?account_id={ACCOUNT}&q=工作").text
    assert "工作筆記" in html and "工作區頂層" in html and "在頁面「工作筆記」底下" in html
    panel = client.get(f"/notion/panel?account_id={ACCOUNT}").text
    assert "自動建立資料庫" in panel and "新增 1 個頁面與 1 個資料庫，不會修改或刪除任何既有內容" in panel


def test_unconnected_panel_shows_oauth_setup_steps(tmp_path: Path) -> None:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "w.db", NOTION_TOKEN=""), allowed_hosts={"testserver"})
    html = TestClient(app).get(f"/notion/panel?account_id={ACCOUNT}").text
    assert "notion.so/profile/integrations" in html and "http://127.0.0.1:31026/notion/oauth/callback" in html
    assert "讀取內容" in html and "插入內容" in html and "NOTION_CLIENT_SECRET" in html
    assert "貼上 Token" in html and "連線" in html


# ---- NotionSyncService：select 型 / status 型「狀態」 ----

class SyncFake:
    def __init__(self, status_type: str | None) -> None:
        self.status_type = status_type
        self.create_payload: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, method = str(request.url), request.method
        if method == "GET" and request.url.path.startswith("/v1/databases/"):
            if self.status_type is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"properties": {"狀態": {"type": self.status_type}}})
        if url.endswith("/v1/file_uploads"):
            return httpx.Response(200, json={"id": "f1"})
        if "/send" in url:
            return httpx.Response(200, json={})
        if url.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        if method == "POST" and url.endswith("/v1/pages"):
            self.create_payload = json.loads(request.content)
            return httpx.Response(200, json={"id": "p", "url": "u"})
        return httpx.Response(404)


@pytest.mark.parametrize(("db_type", "expected_key"), [("select", "select"), ("status", "status"), (None, "status")])
def test_sync_writes_status_in_the_databases_actual_format(tmp_path: Path, db_type: str | None, expected_key: str) -> None:
    pkg, _, zh = produce_carousel_with_qa_retry(slot=ContentSlot.NOON_1130, post_id="p-s", scheduled_date_str="2026-10-25", output_base_dir=tmp_path)
    fake = SyncFake(db_type)
    svc = NotionSyncService(AppSettings(NOTION_TOKEN="t", NOTION_DATABASE_ID="d" * 32), transport=httpx.MockTransport(fake))
    svc.sync_post_to_notion(pkg, zh_media_dir=zh)
    prop = fake.create_payload["properties"]["狀態"]
    assert list(prop) == [expected_key] and prop[expected_key]["name"] == "圖片完成"


def test_shared_lock_reads_select_and_status_status_pages() -> None:
    for shape in ({"select": {"name": "已發布"}}, {"status": {"name": "已排程"}}):
        def handler(request: httpx.Request, shape: dict = shape) -> httpx.Response:
            return httpx.Response(200, json={"results": [{"id": "pg", "properties": {"狀態": shape}}]})

        svc = NotionSyncService(AppSettings(NOTION_TOKEN="t", NOTION_DATABASE_ID="d" * 32), transport=httpx.MockTransport(handler))
        locked, reason = svc.is_slot_already_published_or_locked_in_notion(scheduled_date="2026-10-25", time_slot_str="11:30")
        assert locked and "共享鎖攔截" in reason
