"""Vercel 授權頁面程式（api/index.py）：只要求申請過的權限、只傳到收件匣草稿、轉址與錯誤頁。不碰真實 TikTok。"""

from __future__ import annotations

import http.client
import io
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest

import api.index as handler_mod


@contextmanager
def server() -> Iterator[int]:
    srv = HTTPServer(("127.0.0.1", 0), handler_mod.handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def request(port: int, method: str, path: str, body: str = "") -> tuple[int, dict[str, str], str]:
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=body or None, headers={"Content-Type": "application/x-www-form-urlencoded"} if body else {})
    r = conn.getresponse()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode("utf-8")


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_login_requests_only_the_scopes_declared_in_production() -> None:
    """網站自己的授權入口只能要 user.info.basic 與 video.upload，不能多要直接發布的 video.publish。"""
    with server() as port:
        status, headers, _ = request(port, "GET", "/api/index.py?__path=auth/login")
    assert status == 302
    q = parse_qs(urlparse(headers["location"]).query)
    assert q["scope"] == ["user.info.basic,video.upload"]
    assert "studio.carry-digital-nomad.in.net" in q["redirect_uri"][0] and "autotok" not in q["redirect_uri"][0]


def test_publish_button_only_sends_a_draft_to_the_inbox() -> None:
    """發布測試按鈕只能走 MEDIA_UPLOAD（傳到收件匣草稿），不得帶直接發布與公開隱私的欄位。"""
    sent: dict[str, object] = {}

    def fake_urlopen(req, timeout=0):  # noqa: ANN001, ANN202
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(json.dumps({"data": {"publish_id": "p_inbox_x"}, "error": {"code": "ok"}}).encode())

    with server() as port, mock.patch.object(handler_mod.urllib.request, "urlopen", fake_urlopen):
        status, _, html = request(port, "POST", "/api/index.py?__path=publish", "token=act.abc")
    body = sent["body"]
    assert status == 200 and sent["url"].endswith("/v2/post/publish/content/init/")  # type: ignore[union-attr]
    assert body["post_mode"] == "MEDIA_UPLOAD" and body["media_type"] == "PHOTO"  # type: ignore[index]
    assert "privacy_level" not in body["post_info"] and "PUBLIC_TO_EVERYONE" not in json.dumps(body)  # type: ignore[index,operator]
    assert "收件匣" in html and "p_inbox_x" in html and "發布成功" not in html


def test_publish_requires_a_token() -> None:
    with server() as port:
        status, _, _ = request(port, "POST", "/api/index.py?__path=publish", "")
    assert status == 400


@pytest.mark.parametrize("path", ["/api/index.py?__path=tiktok/callback&code=a&state=b"])
def test_callback_relay_goes_to_the_local_console(path: str) -> None:
    with server() as port:
        status, headers, _ = request(port, "GET", path)
    assert status == 302 and headers["location"] == "http://127.0.0.1:31026/tiktok/oauth/callback?code=a&state=b"


def test_authorised_page_never_shows_the_full_access_token() -> None:
    """授權成功頁只顯示 token 前幾碼；完整 token 只出現在發布測試表單的隱藏欄位，不顯示在畫面上。"""
    token = "act.ABCDEFGHIJKLMNOP1234567890"

    def fake_urlopen(req, timeout=0):  # noqa: ANN001, ANN202
        return FakeResponse(json.dumps({"access_token": token, "open_id": "oid", "expires_in": 86400}).encode())

    with mock.patch.dict("os.environ", {"TIKTOK_CLIENT_SECRET": "s"}), mock.patch.object(handler_mod, "CLIENT_SECRET", "s"):
        with server() as port, mock.patch.object(handler_mod.urllib.request, "urlopen", fake_urlopen):
            status, _, html = request(port, "GET", "/api/index.py?__path=auth/callback&code=good")
    assert status == 200 and "授權成功" in html
    box = html.split('class="token-box">', 1)[1].split("</div>", 1)[0]
    assert token not in box and box.startswith("act.AB") and "已隱藏" in box
    assert html.count(token) == 1 and f'name="token" value="{token}"' in html  # 完整 token 只在隱藏欄位
