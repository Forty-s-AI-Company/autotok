"""Notion 公開 OAuth。state 隨機產生、存在記憶體、單次使用、10 分鐘過期。"""

from __future__ import annotations

import base64
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import AppSettings
from app.errors import AutotokSystemError, BlockedNeedsAuthError
from app.integrations.notion.client import NOTION_API_BASE, NOTION_VERSION

AUTHORIZE_URL = f"{NOTION_API_BASE}/oauth/authorize"
STATE_TTL_SECONDS = 600
MAX_PENDING_STATES = 50

_states: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def oauth_enabled(settings: AppSettings) -> bool:
    return bool(settings.notion_client_id.strip() and settings.notion_client_secret.strip())


def new_state(account_id: str) -> str:
    state = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        for k in [k for k, (_, exp) in _states.items() if exp < now]:
            del _states[k]
        while len(_states) >= MAX_PENDING_STATES:
            del _states[min(_states, key=lambda k: _states[k][1])]
        _states[state] = (account_id, now + STATE_TTL_SECONDS)
    return state


def pop_state(state: str) -> str | None:
    """驗證並消耗 state；回傳當初的 account_id，無效或已用過回傳 None。"""
    with _lock:
        entry = _states.pop(state, None)
    if entry is None or entry[1] < time.monotonic():
        return None
    return entry[0]


def build_authorize_url(settings: AppSettings, state: str) -> str:
    if not oauth_enabled(settings):
        raise BlockedNeedsAuthError("尚未設定 NOTION_CLIENT_ID 與 NOTION_CLIENT_SECRET。")
    query = urlencode(
        {
            "client_id": settings.notion_client_id.strip(),
            "response_type": "code",
            "owner": "user",
            "redirect_uri": settings.notion_redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    settings: AppSettings, code: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """以授權碼換 access token。回傳 Notion 的 token 回應（含 access_token、workspace_name）。"""
    if not oauth_enabled(settings):
        raise BlockedNeedsAuthError("尚未設定 NOTION_CLIENT_ID 與 NOTION_CLIENT_SECRET。")
    basic = base64.b64encode(
        f"{settings.notion_client_id.strip()}:{settings.notion_client_secret.strip()}".encode()
    ).decode()
    try:
        with httpx.Client(transport=transport, timeout=20.0) as client:
            resp = client.post(
                f"{NOTION_API_BASE}/oauth/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.notion_redirect_uri},
            )
    except httpx.HTTPError as exc:
        raise AutotokSystemError(f"連不上 Notion：{exc}", code="NOTION_NETWORK_ERROR") from exc
    if resp.status_code != 200:
        raise AutotokSystemError(
            f"Notion 登入失敗（HTTP {resp.status_code}），請重新操作一次。", code="NOTION_OAUTH_ERROR"
        )
    data: dict[str, Any] = resp.json()
    if not data.get("access_token"):
        raise AutotokSystemError("Notion 沒有回傳 access token。", code="NOTION_OAUTH_ERROR")
    return data
