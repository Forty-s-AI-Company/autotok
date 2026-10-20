"""TikTok 授權與能力偵測（全部使用 MockTransport）、憑證儲存。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.config import AppSettings
from app.integrations.tiktok import OAuthStates, TikTokClient, capability_from_privacy_options
from app.web.main import create_app


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials, "_keyring_ok", lambda: False)
    monkeypatch.setattr(credentials, "_FALLBACK_PATH", tmp_path / "secrets.json")


def test_credentials_roundtrip_and_permissions(tmp_path: Path) -> None:
    credentials.set_secret("notion:a", "tok-1")
    assert credentials.get_secret("notion:a") == "tok-1"
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    credentials.delete_secret("notion:a")
    assert credentials.get_secret("notion:a") == ""


def test_capability_mapping() -> None:
    assert capability_from_privacy_options(["PUBLIC_TO_EVERYONE", "SELF_ONLY"]) == "public"
    assert capability_from_privacy_options(["SELF_ONLY"]) == "self_only"
    assert capability_from_privacy_options([]) == "export_only"


def test_oauth_state_single_use() -> None:
    states = OAuthStates()
    s = states.issue("acc")
    assert states.consume(s) == "acc"
    assert states.consume(s) is None
    assert states.consume("bogus") is None


def _mock(username: str, options: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "act_x", "refresh_token": "rft_x", "expires_in": 86400, "open_id": "o1"})
        if request.url.path.endswith("/creator_info/query/"):
            assert request.headers["Authorization"] == "Bearer act_x"
            return httpx.Response(200, json={"data": {"creator_username": username, "privacy_level_options": options}, "error": {"code": "ok"}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _client(tmp_path: Path, transport: httpx.MockTransport) -> TestClient:
    cfg = AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", TIKTOK_CLIENT_KEY="ck", TIKTOK_CLIENT_SECRET="cs")
    app = create_app(tmp_path, cfg, allowed_hosts={"testserver"})
    app.state.tiktok_transport = transport
    c = TestClient(app, follow_redirects=False)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    c.app_ref = app  # type: ignore[attr-defined]
    return c


def test_authorize_url_and_callback_binds_capability(tmp_path: Path) -> None:
    c = _client(tmp_path, _mock("carry.vietnam.tw", ["SELF_ONLY"]))
    r = c.post("/tiktok/connect", data={"account_id": "carry-vietnam-tw"})
    url = r.headers["HX-Redirect"]
    q = parse_qs(urlparse(url).query)
    assert q["client_key"] == ["ck"] and "video.publish" in q["scope"][0] and q["redirect_uri"][0].endswith("/tiktok/oauth/callback")
    state = q["state"][0]

    cb = c.get(f"/tiktok/oauth/callback?code=abc&state={state}")
    assert cb.status_code == 303 and "tiktok=ok" in cb.headers["location"]
    repo = c.app_ref.state.repo  # type: ignore[attr-defined]
    b = repo.get_tiktok_binding("carry-vietnam-tw")
    assert b["publish_capability"] == "self_only" and b["token_ref"] == "tiktok:carry-vietnam-tw"
    assert "act_x" not in str(b)  # SQLite 不存 token
    assert "act_x" in credentials.get_secret("tiktok:carry-vietnam-tw")
    # state 單次使用
    again = c.get(f"/tiktok/oauth/callback?code=abc&state={state}")
    assert "state_invalid" in again.headers["location"]


def test_callback_rejects_account_mismatch(tmp_path: Path) -> None:
    c = _client(tmp_path, _mock("someone.else", ["SELF_ONLY"]))
    state = parse_qs(urlparse(c.post("/tiktok/connect", data={"account_id": "carry-vietnam-tw"}).headers["HX-Redirect"]).query)["state"][0]
    cb = c.get(f"/tiktok/oauth/callback?code=abc&state={state}")
    assert "tiktok=failed" in cb.headers["location"]
    assert c.app_ref.state.repo.get_tiktok_binding("carry-vietnam-tw") is None  # type: ignore[attr-defined]


def test_connect_requires_app_credentials(tmp_path: Path) -> None:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", TIKTOK_CLIENT_KEY="", TIKTOK_CLIENT_SECRET=""), allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    r = c.post("/tiktok/connect", data={"account_id": "carry-vietnam-tw"})
    assert "HX-Redirect" not in r.headers and "err" in r.headers["HX-Trigger"]


def test_api_error_is_readable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "token expired"}})

    client = TikTokClient(AppSettings(TIKTOK_CLIENT_KEY="ck", TIKTOK_CLIENT_SECRET="cs"), transport=httpx.MockTransport(handler))
    with pytest.raises(Exception, match="token expired"):
        client.creator_info("x")


def test_library_export_writes_pack_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import ContentSlot
    from app.production import produce_carousel_with_qa_retry

    monkeypatch.chdir(tmp_path)
    cfg = AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", EXPORT_BASE_DIR=tmp_path / "exports")
    app = create_app(tmp_path, cfg, allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130, post_id="exp-1", scheduled_date_str="2026-10-21", output_base_dir=tmp_path / "artifacts" / "d"
    )
    app.state.repo.upsert_post(pkg)
    r = c.post("/library/export", data={"post_id": "exp-1"})
    assert "exports" in r.headers["HX-Trigger"] or "\\u5df2\\u532f\\u51fa" in r.headers["HX-Trigger"]
    assert (tmp_path / "exports" / "exp-1" / "manifest.json").is_file()
