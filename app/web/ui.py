"""Jinja 環境、共用濾鏡與頁面共用 context。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.web.status import system_status

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

NAV_ITEMS: list[tuple[str, str, str, str]] = [
    ("today", "/", "今天", "i-today"),
    ("research", "/research", "研究", "i-search"),
    ("studio", "/studio", "製作", "i-studio"),
    ("library", "/library", "內容庫", "i-lib"),
    ("analytics", "/analytics", "成效", "i-chart"),
    ("settings", "/settings", "設定", "i-set"),
]


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def local_dt(value: str | None, tz_name: str = "Asia/Taipei") -> str:
    """ISO 時間 → `MM/DD HH:MM`（帳號時區）。空值回傳空字串。"""
    dt = parse_iso(value)
    if dt is None:
        return ""
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%m/%d %H:%M")


def _fresh_filter(value: str | None, tz_name: str = "Asia/Taipei") -> str:
    return local_dt(value, tz_name)


templates.env.filters["localdt"] = _fresh_filter
templates.env.globals["NAV_ITEMS"] = NAV_ITEMS


def page_context(request: Request, nav: str, title: str, **extra: object) -> dict[str, object]:
    state = request.app.state
    repo = state.repo
    accounts = repo.list_accounts()
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    return {
        "request": request,
        "nav": nav,
        "title": title,
        "csrf": state.csrf_token,
        "accounts": accounts,
        "active": active,
        "market": market,
        "tz": market.timezone,
        "system": system_status(repo, state.settings, active.account_id),
        "job": state.jobs.current(),
        **extra,
    }


def render(request: Request, name: str, nav: str, title: str, **extra: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, page_context(request, nav, title, **extra))


def toast(response: Response, msg: str, kind: str = "ok", **extra: object) -> Response:
    """以 HX-Trigger 通知前端顯示提示。`refresh=True` 讓頁面重新載入；`events=[...]` 另外觸發自訂事件。"""
    payload: dict[str, object] = {"toast": {"kind": kind, "msg": msg}}
    if extra.get("refresh"):
        payload["pageRefresh"] = True
    for name in extra.get("events", []) or []:  # type: ignore[union-attr]
        payload[str(name)] = True
    response.headers["HX-Trigger"] = json.dumps(payload)  # 標頭只能是 latin-1，中文用 \uXXXX 跳脫
    return response
