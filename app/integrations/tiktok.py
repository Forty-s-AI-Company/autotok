"""TikTok 官方 API：Login Kit 授權與發布能力偵測（只讀，不發文）。

尚未用真實 TikTok 帳號驗證：
- Redirect URI 必須與 Developer Portal 登記的完全一致；TikTok 是否接受 `http://127.0.0.1` 需實測。
- `creator_info/query` 回傳的 `privacy_level_options` 決定帳號目前能用的隱私等級，
  只有 `SELF_ONLY` → 僅自己可見；含 `PUBLIC_TO_EVERYONE` → 可公開。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import AppSettings
from app.errors import AutotokSystemError, BlockedNeedsAuthError

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
CREATOR_INFO_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
PHOTO_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/content/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
SCOPES = "user.info.basic,video.publish,video.upload"
_STATE_TTL = 600


def capability_from_privacy_options(options: list[str]) -> str:
    """把 creator_info 的 privacy_level_options 轉成 `public` / `self_only` / `export_only`。"""
    if "PUBLIC_TO_EVERYONE" in options:
        return "public"
    if "SELF_ONLY" in options:
        return "self_only"
    return "export_only"


@dataclass
class TikTokTokens:
    access_token: str
    refresh_token: str
    expires_at: float
    open_id: str = ""

    def dumps(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def loads(cls, raw: str) -> TikTokTokens | None:
        try:
            d = json.loads(raw)
            return cls(str(d["access_token"]), str(d.get("refresh_token", "")), float(d["expires_at"]), str(d.get("open_id", "")))
        except (ValueError, KeyError, TypeError):
            return None


class OAuthStates:
    """單次使用、有時效的 OAuth state（記憶體內）。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def issue(self, account_id: str) -> str:
        state = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._items = {k: v for k, v in self._items.items() if now - v[1] < _STATE_TTL}
            self._items[state] = (account_id, now)
        return state

    def consume(self, state: str) -> str | None:
        with self._lock:
            item = self._items.pop(state, None)
        if item is None or time.time() - item[1] >= _STATE_TTL:
            return None
        return item[0]


class TikTokClient:
    def __init__(self, settings: AppSettings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport

    def _require_app(self) -> None:
        if not self.settings.tiktok_client_key.strip() or not self.settings.tiktok_client_secret.strip():
            raise BlockedNeedsAuthError("尚未設定 TIKTOK_CLIENT_KEY 與 TIKTOK_CLIENT_SECRET（TikTok Developer Portal 的 App 憑證）。")

    def build_authorize_url(self, state: str) -> str:
        self._require_app()
        params = {
            "client_key": self.settings.tiktok_client_key,
            "scope": SCOPES,
            "response_type": "code",
            "redirect_uri": self.settings.tiktok_redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    def _post(self, url: str, **kwargs: object) -> dict[str, object]:
        try:
            with httpx.Client(transport=self._transport, timeout=20) as c:
                r = c.post(url, **kwargs)  # type: ignore[arg-type]
        except httpx.HTTPError as exc:
            raise AutotokSystemError(f"連不上 TikTok：{exc}", code="TIKTOK_NETWORK") from exc
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or (isinstance(data.get("error"), dict) and data["error"].get("code") not in (None, "ok")):
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            msg = str(err.get("message") or data.get("error_description") or r.text[:200])
            if r.status_code in (401, 403):
                raise BlockedNeedsAuthError(f"TikTok 拒絕授權：{msg}")
            raise AutotokSystemError(f"TikTok API 錯誤（{r.status_code}）：{msg}", code="TIKTOK_API")
        return data

    def _tokens_from(self, data: dict[str, object]) -> TikTokTokens:
        if "access_token" not in data:
            raise AutotokSystemError("TikTok 沒有回傳 access_token", code="TIKTOK_API")
        return TikTokTokens(
            access_token=str(data["access_token"]),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=time.time() + float(data.get("expires_in", 86400)),  # type: ignore[arg-type]
            open_id=str(data.get("open_id", "")),
        )

    def exchange_code(self, code: str) -> TikTokTokens:
        self._require_app()
        data = self._post(
            TOKEN_URL,
            data={
                "client_key": self.settings.tiktok_client_key,
                "client_secret": self.settings.tiktok_client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.settings.tiktok_redirect_uri,
            },
        )
        return self._tokens_from(data)

    def refresh(self, refresh_token: str) -> TikTokTokens:
        self._require_app()
        data = self._post(
            TOKEN_URL,
            data={
                "client_key": self.settings.tiktok_client_key,
                "client_secret": self.settings.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        return self._tokens_from(data)

    def creator_info(self, access_token: str) -> dict[str, object]:
        """回傳 `data` 內容：creator_username、privacy_level_options…"""
        data = self._post(
            CREATOR_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
        )
        inner = data.get("data")
        return inner if isinstance(inner, dict) else {}

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"}

    def init_photo_post(self, access_token: str, payload: dict[str, object]) -> str:
        """送出照片貼文 init，回傳 publish_id。API 明確拒絕時丟 TIKTOK_API；連線中斷丟 TIKTOK_NETWORK（結果不明）。"""
        data = self._post(PHOTO_INIT_URL, headers=self._auth_headers(access_token), json=payload)
        inner = data.get("data")
        publish_id = str(inner.get("publish_id", "")) if isinstance(inner, dict) else ""
        if not publish_id:
            raise AutotokSystemError("TikTok 沒有回傳 publish_id", code="TIKTOK_API")
        return publish_id

    def fetch_status(self, access_token: str, publish_id: str) -> dict[str, object]:
        data = self._post(STATUS_URL, headers=self._auth_headers(access_token), json={"publish_id": publish_id})
        inner = data.get("data")
        return inner if isinstance(inner, dict) else {}
