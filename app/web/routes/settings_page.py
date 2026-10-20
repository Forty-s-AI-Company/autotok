"""設定：帳號與市場、Notion、TikTok、生圖與文案引擎。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.engines import codex_status
from app.markets import MARKET_TEMPLATE_LABELS, SLOT_KINDS, profile_from_template
from app.models import TikTokAccountProfile
from app.web.ui import render, toast

router = APIRouter()

HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._]{2,24}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
TIMEZONES = [
    "Asia/Taipei", "Asia/Tokyo", "Asia/Seoul", "Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Jakarta",
    "Asia/Singapore", "Asia/Manila", "Europe/London", "America/Los_Angeles",
]  # fmt: skip
LANGUAGES = [
    ("vi", "越南文", "latin"), ("th", "泰文", "thai"), ("id", "印尼文", "latin"),
    ("en", "英文", "latin"), ("ja", "日文", "cjk"), ("ko", "韓文", "cjk"),
]  # fmt: skip
CAPABILITY_LABEL = {"public": "可公開發布", "self_only": "僅自己可見", "export_only": "僅匯出"}


def _refresh(msg: str, kind: str = "ok") -> Response:
    resp = Response(status_code=200)
    return toast(resp, msg, kind, refresh=True)


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in value.splitlines() if ln.strip()]


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, account: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    accounts = repo.list_accounts()
    selected = repo.get_account(account) if account else None
    selected = selected or repo.get_active_account()
    market = repo.get_market_profile(selected.account_id)
    rows = []
    for a in accounts:
        m = repo.get_market_profile(a.account_id)
        nb = repo.get_notion_binding(a.account_id)
        tk = repo.get_tiktok_binding(a.account_id)
        rows.append(
            {
                "a": a,
                "market": m,
                "notion": nb,
                "cap": CAPABILITY_LABEL.get(str(tk["publish_capability"]) if tk else "export_only", "僅匯出"),
                "cap_kind": "ok" if tk and tk["publish_capability"] == "public" else "warn",
            }
        )
    tiktok = repo.get_tiktok_binding(selected.account_id)
    return render(
        request,
        "settings.html",
        "settings",
        "設定",
        rows=rows,
        selected=selected,
        sel_market=market,
        sel_notion=repo.get_notion_binding(selected.account_id),
        tiktok=tiktok,
        cap_label=CAPABILITY_LABEL.get(str(tiktok["publish_capability"]) if tiktok else "export_only", "僅匯出"),
        engine=codex_status(cfg.codex_bin),
        codex_model=cfg.codex_model,
        market_templates=MARKET_TEMPLATE_LABELS,
        timezones=TIMEZONES,
        languages=LANGUAGES,
        slot_kinds=SLOT_KINDS,
        has_env_notion_token=bool(cfg.notion_token.strip()),
    )


@router.post("/accounts/create")
def create_account(
    request: Request,
    handle: str = Form(...),
    display_name: str = Form(""),
    template: str = Form("vi-tw"),
) -> Response:
    repo = request.app.state.repo
    handle = handle.strip()
    if not HANDLE_RE.match(handle):
        return toast(Response(status_code=200), "帳號名稱只能包含英數字、點與底線（2–24 字）", "err")
    if not handle.startswith("@"):
        handle = "@" + handle
    account_id = handle.lstrip("@").lower().replace(".", "-").replace("_", "-")
    if repo.get_account(account_id):
        return toast(Response(status_code=200), "這個帳號已經存在", "err")
    profile = profile_from_template(template)
    repo.upsert_account(
        TikTokAccountProfile(
            account_id=account_id,
            handle=handle,
            display_name=display_name.strip() or handle,
            market_focus=profile.audience,
            target_language=profile.target_language,
            notion_database_id="",
            is_active=True,
        )
    )
    repo.save_market_profile(account_id, profile)
    return _refresh(f"已新增 {handle}")


@router.post("/accounts/delete")
def delete_account(request: Request, account_id: str = Form(...)) -> Response:
    repo = request.app.state.repo
    if len(repo.list_accounts()) <= 1:
        return toast(Response(status_code=200), "至少要保留一個帳號", "err")
    if not repo.delete_account(account_id):
        return toast(Response(status_code=200), "這個帳號已有貼文，不能刪除", "err")
    return _refresh("已刪除帳號")


@router.post("/settings/market")
async def save_market(request: Request) -> Response:
    repo = request.app.state.repo
    form = await request.form()
    account_id = str(form.get("account_id", ""))
    acc = repo.get_account(account_id)
    if acc is None:
        return toast(Response(status_code=200), "找不到帳號", "err")
    profile = repo.get_market_profile(account_id)

    lang = str(form.get("target_language", profile.target_language))
    lang_row = next((row for row in LANGUAGES if row[0] == lang), None)
    if lang_row:
        profile.target_language, profile.target_language_label, profile.script = lang_row[0], lang_row[1], lang_row[2]
    tz = str(form.get("timezone", profile.timezone))
    if tz in TIMEZONES:
        profile.timezone = tz
    for kind in SLOT_KINDS:
        t = str(form.get(f"time_{kind.value}", "")).strip()
        if t:
            if not TIME_RE.match(t):
                return toast(Response(status_code=200), f"時間格式不正確：{t}（請用 24 小時制 HH:MM）", "err")
            profile.slot_times[kind.value] = t
        name = str(form.get(f"track_{kind.value}", "")).strip()
        if name:
            profile.track_names[kind.value] = name
    profile.audience = str(form.get("audience", profile.audience)).strip()
    profile.tone = str(form.get("tone", profile.tone)).strip() or profile.tone
    profile.content_brief = str(form.get("content_brief", profile.content_brief)).strip()[:2000]
    profile.scene_style = str(form.get("scene_style", profile.scene_style)).strip()
    profile.compliance_notes = _lines(str(form.get("compliance_notes", "")))
    profile.seed_keywords = _lines(str(form.get("seed_keywords", "")))
    repo.save_market_profile(account_id, profile)

    repo.upsert_account(
        acc.model_copy(update={"market_focus": profile.audience or acc.market_focus, "target_language": profile.target_language})
    )
    return toast(Response(status_code=200), "已儲存市場設定")
