"""Notion 整合：用戶端、欄位檢查、OAuth state、每帳號 Token 與設定頁面板（全部使用 MockTransport）。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.config import AppSettings
from app.errors import AutotokSystemError, BlockedNeedsAuthError
from app.integrations.notion import binding as nb
from app.integrations.notion import oauth
from app.integrations.notion.client import NotionClient
from app.integrations.notion.schema_check import check_schema
from app.notion_schema import REQUIRED_NOTION_PROPERTIES
from app.notion_sync import NotionSyncService
from app.state import PostStateRepository
from app.web.main import create_app

DB1 = "1" * 32
ACCOUNT = "carry-vietnam-tw"


@pytest.fixture(autouse=True)
def memory_secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """不碰真實金鑰圈：以記憶體字典取代。"""
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "set_secret", lambda ref, value, **_: store.__setitem__(ref, value))
    monkeypatch.setattr(credentials, "get_secret", lambda ref, **_: store.get(ref, ""))
    monkeypatch.setattr(credentials, "delete_secret", lambda ref, **_: store.pop(ref, None))
    return store


def _db_payload(missing: set[str] = frozenset(), wrong: dict[str, str] | None = None) -> dict:
    props = {}
    for name, ptype in REQUIRED_NOTION_PROPERTIES.items():
        if name in missing:
            continue
        props[name] = {"id": name, "type": (wrong or {}).get(name, ptype)}
    return {"object": "database", "id": DB1, "title": [{"plain_text": "TikTok 輪播內容"}], "properties": props}


def test_search_parses_databases_and_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer tok"
        if body["filter"]["value"] == "database":
            return httpx.Response(200, json={"results": [
                {"object": "database", "id": "d1", "title": [{"plain_text": "內容"}], "url": "u", "last_edited_time": "2026-10-20T01:00:00Z"}
            ]})
        return httpx.Response(200, json={"results": [
            {"object": "page", "id": "p1", "properties": {"Name": {"type": "title", "title": [{"plain_text": "靈感"}]}}, "url": "u2", "last_edited_time": ""}
        ]})

    client = NotionClient("tok", transport=httpx.MockTransport(handler))
    assert client.search("database")[0]["title"] == "內容"
    assert client.search("page")[0]["title"] == "靈感"


@pytest.mark.parametrize(("status", "exc"), [(401, BlockedNeedsAuthError), (404, AutotokSystemError), (429, AutotokSystemError)])
def test_client_error_mapping(status: int, exc: type[Exception]) -> None:
    client = NotionClient("tok", transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})))
    with pytest.raises(exc):
        client.whoami()


def test_schema_check_reports_missing_and_mismatch() -> None:
    check = check_schema(_db_payload(missing={"CTA", "狀態"}, wrong={"越南文標題": "title"})["properties"])
    assert not check.ok
    assert {m.name for m in check.missing} == {"CTA", "狀態"}
    assert [m.name for m in check.creatable_missing] == ["CTA"]  # status 不能由 API 新增
    assert check.type_mismatch[0].name == "越南文標題"
    assert check_schema(_db_payload()["properties"]).ok


def test_add_properties_sends_only_new_fields() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"method": request.method, "body": json.loads(request.content)})
        return httpx.Response(200, json={})

    client = NotionClient("tok", transport=httpx.MockTransport(handler))
    client.add_properties(DB1, {"CTA": "rich_text"})
    assert seen == [{"method": "PATCH", "body": {"properties": {"CTA": {"rich_text": {}}}}}]
    with pytest.raises(AutotokSystemError):
        client.add_properties(DB1, {"狀態": "status"})
    assert len(seen) == 1


def test_oauth_state_is_single_use() -> None:
    state = oauth.new_state(ACCOUNT)
    assert oauth.pop_state(state) == ACCOUNT
    assert oauth.pop_state(state) is None
    assert oauth.pop_state("never-issued") is None


def test_oauth_authorize_url_and_exchange() -> None:
    settings = AppSettings(NOTION_CLIENT_ID="cid", NOTION_CLIENT_SECRET="sec")
    url = oauth.build_authorize_url(settings, "st")
    assert "client_id=cid" in url and "state=st" in url and "127.0.0.1%3A8765" in url

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "at", "workspace_name": "WS"})

    data = oauth.exchange_code(settings, "code", transport=httpx.MockTransport(handler))
    assert data["workspace_name"] == "WS"
    with pytest.raises(BlockedNeedsAuthError):
        oauth.build_authorize_url(AppSettings(NOTION_CLIENT_ID="", NOTION_CLIENT_SECRET=""), "st")


def test_token_goes_to_secret_store_not_sqlite(tmp_path: Path, memory_secrets: dict[str, str]) -> None:
    db = tmp_path / "t.db"
    repo = PostStateRepository(db)
    nb.store_connection(repo, ACCOUNT, token="ntn_super_secret", auth_method="token", workspace_name="WS")
    assert memory_secrets[f"notion:{ACCOUNT}"] == "ntn_super_secret"
    assert b"ntn_super_secret" not in db.read_bytes()
    assert repo.get_notion_binding(ACCOUNT)["token_ref"] == f"notion:{ACCOUNT}"


def test_for_account_returns_none_without_binding_or_token(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "t.db")
    settings = AppSettings(NOTION_TOKEN="", SQLITE_DB_PATH=tmp_path / "t.db")
    assert NotionSyncService.for_account(repo, settings, ACCOUNT) is None
    nb.store_connection(repo, ACCOUNT, token="tok", auth_method="token", workspace_name="WS")
    assert NotionSyncService.for_account(repo, settings, ACCOUNT) is None  # 有 token 但尚未選資料庫
    repo.save_notion_binding(ACCOUNT, database_id="db-123")
    svc = NotionSyncService.for_account(repo, settings, ACCOUNT)
    assert svc is not None and svc._db_id() == "db-123" and svc._require_token() == "tok"
    # 非預設帳號不會退回全域 Token
    other = AppSettings(NOTION_TOKEN="global", SQLITE_DB_PATH=tmp_path / "t.db")
    assert NotionSyncService.for_account(repo, other, "some-other-account") is None


def test_for_account_global_token_fallback_for_default_account(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "t.db")
    settings = AppSettings(NOTION_TOKEN="global-tok", NOTION_DATABASE_ID="legacy-db", SQLITE_DB_PATH=tmp_path / "t.db")
    svc = NotionSyncService.for_account(repo, settings, ACCOUNT)
    assert svc is not None and svc._db_id() == "legacy-db"


def _web(tmp_path: Path, handler) -> tuple[TestClient, object]:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "w.db", NOTION_TOKEN=""), allowed_hosts={"testserver"})
    app.state.notion_transport = httpx.MockTransport(handler)
    return TestClient(app), app


def test_web_connect_bind_and_add_fields_flow(tmp_path: Path) -> None:
    state = {"db": _db_payload(missing={"CTA"}), "patched": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/users/me":
            return httpx.Response(200, json={"name": "bot", "bot": {"workspace_name": "Carry WS"}})
        if path == "/v1/search":
            kind = json.loads(request.content)["filter"]["value"]
            if kind == "database":
                return httpx.Response(200, json={"results": [{"object": "database", "id": DB1, "title": [{"plain_text": "TikTok 輪播內容"}], "url": "", "last_edited_time": "2026-10-20T01:00:00Z"}]})
            return httpx.Response(200, json={"results": []})
        if path == "/v1/databases/" + DB1 and request.method == "GET":
            return httpx.Response(200, json=state["db"])
        if path == "/v1/databases/" + DB1 and request.method == "PATCH":
            body = json.loads(request.content)
            state["patched"].append(body)
            state["db"]["properties"]["CTA"] = {"id": "x", "type": "rich_text"}
            return httpx.Response(200, json={})
        return httpx.Response(500)

    client, app = _web(tmp_path, handler)
    hdr = {"X-AutoTok-Token": app.state.csrf_token}

    # 未連線的面板
    assert "貼上 Token" in client.get(f"/notion/panel?account_id={ACCOUNT}").text
    # 沒帶安全 token 會被拒絕
    assert client.post("/notion/connect-token", data={"account_id": ACCOUNT, "token": "t"}).status_code == 403

    r = client.post("/notion/connect-token", data={"account_id": ACCOUNT, "token": "ntn_x"}, headers=hdr)
    assert r.status_code == 200 and "notionChanged" in r.headers["HX-Trigger"]
    assert "ntn_x" not in client.get(f"/notion/panel?account_id={ACCOUNT}").text

    r = client.post("/notion/bind-database", data={"account_id": ACCOUNT, "database_id": DB1}, headers=hdr)
    assert r.status_code == 200
    binding = app.state.repo.get_notion_binding(ACCOUNT)
    assert binding["database_id"] == DB1 and binding["schema_missing"] == ["CTA"]
    panel = client.get(f"/notion/panel?account_id={ACCOUNT}").text
    assert "缺少「CTA」" in panel and "新增缺少的欄位" in panel and "既有欄位與資料不會變動" in panel

    r = client.post("/notion/add-fields", data={"account_id": ACCOUNT}, headers=hdr)
    assert r.status_code == 200
    assert state["patched"] == [{"properties": {"CTA": {"rich_text": {}}}}]
    assert app.state.repo.get_notion_binding(ACCOUNT)["schema_missing"] == []

    r = client.post("/notion/disconnect", data={"account_id": ACCOUNT}, headers=hdr)
    assert app.state.repo.get_notion_binding(ACCOUNT) is None
    assert client.get("/settings").status_code == 200


def test_web_oauth_callback_rejects_unknown_state(tmp_path: Path) -> None:
    client, _ = _web(tmp_path, lambda r: httpx.Response(500))
    assert client.get("/notion/oauth/callback?code=c&state=bogus", follow_redirects=False).status_code == 400
    assert client.get("/notion/oauth/start?account_id=" + ACCOUNT, follow_redirects=False).status_code == 400  # 未設定 client id
