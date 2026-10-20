"""本機伺服器防護：只接受本機 Host，並要求所有寫入請求帶有每次啟動隨機產生的 token。

目的：避免任何第三方網頁（CSRF）或 DNS rebinding 對 127.0.0.1 的 API 發出寫入請求，
進而消耗生圖額度或寫入 Notion。
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
CSRF_HEADER = "x-autotok-token"
ACCOUNT_HEADER = "x-autotok-account"
ACCOUNT_EXEMPT_PATHS = {"/accounts/switch", "/accounts/create"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _host_only(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.split(":")[0]


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, token: str, allowed_hosts: Iterable[str] | None = None) -> None:  # noqa: ANN001
        super().__init__(app)
        self.token = token
        self.allowed_hosts = set(allowed_hosts) if allowed_hosts is not None else set(LOCAL_HOSTS)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host_header = request.headers.get("host", "")
        if _host_only(host_header) not in self.allowed_hosts:
            return PlainTextResponse("不允許的 Host", status_code=403)
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc.lower() != host_header.lower():
                return PlainTextResponse("跨網站請求已被拒絕", status_code=403)
            supplied = request.headers.get(CSRF_HEADER, "")
            if not secrets.compare_digest(supplied.encode("utf-8", "replace"), self.token.encode()):
                return PlainTextResponse("缺少或錯誤的安全 token，請重新整理頁面", status_code=403)
            # 頁面載入時的帳號必須仍是目前作用中的帳號，避免多分頁切換後動作打到別的帳號
            page_account = request.headers.get(ACCOUNT_HEADER)
            repo = getattr(request.app.state, "repo", None)
            if page_account and repo is not None and request.url.path not in ACCOUNT_EXEMPT_PATHS:
                if repo.get_active_account().account_id != page_account:
                    return PlainTextResponse("帳號已在其他分頁切換，請重新整理頁面後再操作", status_code=409)
        return await call_next(request)
