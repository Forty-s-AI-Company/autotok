"""Telegram 通知：全部使用 MockTransport，不連真實 Telegram。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.config import AppSettings
from app.notify import TOKEN_REF, detect_chats, send_notification
from app.state import PostStateRepository
from app.web.jobs import JobRunner
from app.web.main import create_app

TOKEN = "123456:SECRET-TOKEN-XYZ9"


class Recorder:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.calls: list[httpx.Request] = []
        self.status, self.body = status, body if body is not None else {"ok": True, "result": {}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _setup(tmp_path: Path, *, enabled: bool = True, on_success: bool = False, token: str = TOKEN):
    repo = PostStateRepository(tmp_path / "t.db")
    repo.set_app_setting("telegram_enabled", "1" if enabled else "0")
    repo.set_app_setting("telegram_chat_id", "42")
    repo.set_app_setting("notify_on_success", "1" if on_success else "0")
    if token:
        credentials.set_secret(TOKEN_REF, token)
    return repo, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db")


def test_disabled_sends_nothing(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path, enabled=False)
    rec = Recorder()
    assert send_notification(repo, cfg, title="t", message="m", transport=rec.transport) is False
    assert rec.calls == []


def test_incomplete_config_sends_nothing(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path, token="")
    rec = Recorder()
    assert send_notification(repo, cfg, title="t", message="m", transport=rec.transport) is False
    repo.set_app_setting("telegram_chat_id", "")
    credentials.set_secret(TOKEN_REF, TOKEN)
    assert send_notification(repo, cfg, title="t", message="m", transport=rec.transport) is False
    assert rec.calls == []


def test_enabled_sends_formatted_plain_text(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path)
    rec = Recorder()
    assert send_notification(repo, cfg, title="製作失敗", message="原因 sessionid=abc123", transport=rec.transport) is True
    req = rec.calls[0]
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body["chat_id"] == "42" and "parse_mode" not in body
    assert body["text"].startswith("[AutoTok] 製作失敗\n") and "abc123" not in body["text"]


def test_info_level_respects_notify_on_success(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path, on_success=False)
    rec = Recorder()
    assert send_notification(repo, cfg, title="t", message="m", level="info", transport=rec.transport) is False
    assert send_notification(repo, cfg, title="t", message="m", level="error", transport=rec.transport) is True
    repo.set_app_setting("notify_on_success", "1")
    assert send_notification(repo, cfg, title="t", message="m", level="info", transport=rec.transport) is True


def test_long_message_truncated(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path)
    rec = Recorder()
    send_notification(repo, cfg, title="t", message="x" * 10000, transport=rec.transport)
    assert len(json.loads(rec.calls[0].content)["text"]) <= 3800


@pytest.mark.parametrize(
    ("status", "body", "expect"),
    [
        (401, {"ok": False, "description": "Unauthorized"}, "Bot Token 無效"),
        (400, {"ok": False, "description": "Bad Request: chat not found"}, "Chat ID 不正確"),
        (403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}, "沒有被使用者啟動過"),
    ],
)
def test_error_classification_logged_without_token(tmp_path: Path, status: int, body: dict, expect: str) -> None:
    repo, cfg = _setup(tmp_path)
    rec = Recorder(status, body)
    assert send_notification(repo, cfg, title="t", message="m", transport=rec.transport) is False
    row = repo.last_job(account_id="", job="notify")
    assert row["status"] == "error" and expect in row["error"] and TOKEN not in json.dumps(row)


def test_network_error_retries_twice_and_never_raises(tmp_path: Path) -> None:
    repo, cfg = _setup(tmp_path)
    calls = []

    def boom(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("down", request=request)

    assert send_notification(repo, cfg, title="t", message="m", transport=httpx.MockTransport(boom)) is False
    assert len(calls) == 2
    assert TOKEN not in json.dumps(repo.last_job(account_id="", job="notify"))


def test_detect_chats_parses_updates() -> None:
    rec = Recorder(body={"ok": True, "result": [
        {"message": {"chat": {"id": 7, "first_name": "Eden", "last_name": "L"}}},
        {"message": {"chat": {"id": 7, "first_name": "Eden"}}},
        {"channel_post": {"chat": {"id": -100, "title": "群組"}}},
        {"edited_message": {}},
    ]})
    chats = detect_chats(TOKEN, transport=rec.transport)
    assert {c["id"] for c in chats} == {"7", "-100"}


def test_job_runner_failure_notifies_and_notify_failure_does_not_break(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, cfg = _setup(tmp_path)
    sent: list[dict] = []
    import app.notify as notify_mod

    monkeypatch.setattr(notify_mod, "send_notification", lambda r, s, **kw: sent.append(kw) or True)
    runner = JobRunner(repo, cfg)

    def boom(h):  # noqa: ANN001
        raise RuntimeError("字型缺失")

    runner.submit(account_id="carry-vietnam-tw", job="x", label="製作 10/21", fn=boom, wait=True)
    assert sent and sent[0]["level"] == "error" and "製作 10/21" in sent[0]["title"] and "字型缺失" in sent[0]["message"]

    def explode(r, s, **kw):  # noqa: ANN001, ANN003
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notify_mod, "send_notification", explode)
    runner.submit(account_id="carry-vietnam-tw", job="x", label="第二個", fn=boom, wait=True)
    assert runner.current() is None
    assert repo.last_job(account_id="carry-vietnam-tw", job="x")["status"] == "error"
    ok = runner.submit(account_id="carry-vietnam-tw", job="y", label="成功", fn=lambda h: "ok", wait=True)
    assert ok and len(sent) == 1  # 成功不通知


def _client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"), allowed_hosts={"testserver"})
    app.state.notify_transport = None
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    c.app_ref = app  # type: ignore[attr-defined]
    return c


def test_panel_save_test_detect_and_never_echo_token(tmp_path: Path) -> None:
    c = _client(tmp_path)
    rec = Recorder(body={"ok": True, "result": [{"message": {"chat": {"id": 9, "first_name": "Me"}}}]})
    c.app_ref.state.notify_transport = rec.transport  # type: ignore[attr-defined]

    bad = c.post("/notifications/save", data={"enabled": "1", "token": "", "chat_id": ""})
    assert "err" in bad.headers["HX-Trigger"]

    ok = c.post("/notifications/save", data={"enabled": "1", "token": TOKEN, "chat_id": "42", "on_success": "1"})
    assert "ok" in ok.headers["HX-Trigger"] and TOKEN not in ok.headers["HX-Trigger"]
    repo = c.app_ref.state.repo  # type: ignore[attr-defined]
    assert repo.get_app_setting("telegram_enabled") == "1" and repo.get_app_setting("notify_on_success") == "1"
    assert TOKEN not in json.dumps(repo.list_jobs(limit=50))
    assert credentials.get_secret(TOKEN_REF) == TOKEN

    html = c.get("/notifications/panel").text
    assert TOKEN not in html and "XYZ9" in html and 'name="chat_id"' in html and 'value="42"' in html
    assert TOKEN not in c.get("/settings").text

    t = c.post("/notifications/test", data={})
    assert "ok" in t.headers["HX-Trigger"] and TOKEN not in t.headers["HX-Trigger"]
    assert any(r.url.path.endswith("/sendMessage") for r in rec.calls)
    assert "傳送測試訊息" in c.get("/notifications/panel").text

    d = c.post("/notifications/detect", data={})
    assert 'data-value="9"' in d.text

    rec.body = {"ok": True, "result": []}
    assert "err" in c.post("/notifications/detect", data={}).headers["HX-Trigger"]
    rec.status, rec.body = 401, {"ok": False, "description": "Unauthorized"}
    fail = c.post("/notifications/test", data={})
    assert "err" in fail.headers["HX-Trigger"] and TOKEN not in fail.headers["HX-Trigger"]
    assert "Bot Token" in c.get("/notifications/panel").text

    off = c.post("/notifications/save", data={"enabled": "", "chat_id": "42"})
    assert repo.get_app_setting("telegram_enabled") == "0" and "ok" in off.headers["HX-Trigger"]
    n = len(rec.calls)
    assert send_notification(repo, c.app_ref.state.settings, title="t", message="m", transport=rec.transport) is False  # type: ignore[attr-defined]
    assert len(rec.calls) == n
