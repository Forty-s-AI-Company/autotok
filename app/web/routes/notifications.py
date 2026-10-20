"""設定 → 通知：Telegram 機器人。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app import credentials
from app.notify import TOKEN_REF, TelegramError, detect_chats, get_config, mask_token, send_text
from app.web.ui import templates, toast

router = APIRouter()


def _transport(request: Request):  # noqa: ANN202
    return getattr(request.app.state, "notify_transport", None)


@router.get("/notifications/panel", response_class=HTMLResponse)
def panel(request: Request) -> HTMLResponse:
    repo = request.app.state.repo
    cfg = get_config(repo)
    token = credentials.get_secret(TOKEN_REF)
    active = repo.get_active_account()
    logs = [j for j in repo.list_jobs(limit=100) if j["job"] == "notify"][:5]
    return templates.TemplateResponse(
        request,
        "_notify_panel.html",
        {
            "request": request,
            "cfg": cfg,
            "masked": mask_token(token),
            "logs": logs,
            "tz": repo.get_market_profile(active.account_id).timezone,
        },
    )


@router.post("/notifications/save")
def save(
    request: Request,
    enabled: str = Form(""),
    token: str = Form(""),
    chat_id: str = Form(""),
    on_success: str = Form(""),
) -> Response:
    repo = request.app.state.repo
    token = token.strip()
    chat_id = chat_id.strip()
    if token:
        credentials.set_secret(TOKEN_REF, token)
    if enabled and not (token or credentials.get_secret(TOKEN_REF)):
        return toast(Response(status_code=200), "要啟用通知，請先填 Bot Token", "err")
    if enabled and not chat_id:
        return toast(Response(status_code=200), "要啟用通知，請先填 Chat ID", "err")
    repo.set_app_setting("telegram_enabled", "1" if enabled else "0")
    repo.set_app_setting("telegram_chat_id", chat_id)
    repo.set_app_setting("notify_on_success", "1" if on_success else "0")
    return toast(Response(status_code=200), "已儲存通知設定", events=["notifyChanged"])


@router.post("/notifications/test")
def test(request: Request, token: str = Form(""), chat_id: str = Form("")) -> Response:
    repo = request.app.state.repo
    tok = token.strip() or credentials.get_secret(TOKEN_REF)
    chat = chat_id.strip() or repo.get_app_setting("telegram_chat_id", "")
    if not tok or not chat:
        return toast(Response(status_code=200), "請先填 Bot Token 與 Chat ID", "err")
    try:
        send_text(tok, chat, "測試訊息", "通知設定正常", transport=_transport(request))
    except TelegramError as exc:
        return toast(Response(status_code=200), f"傳送失敗：{exc}", "err", events=["notifyChanged"])
    return toast(Response(status_code=200), "測試訊息已送出", events=["notifyChanged"])


@router.post("/notifications/detect", response_class=HTMLResponse)
def detect(request: Request, token: str = Form("")) -> Response:
    tok = token.strip() or credentials.get_secret(TOKEN_REF)
    if not tok:
        return toast(HTMLResponse(""), "請先填 Bot Token", "err")
    try:
        chats = detect_chats(tok, transport=_transport(request))
    except TelegramError as exc:
        return toast(HTMLResponse(""), f"偵測失敗：{exc}", "err")
    if not chats:
        return toast(HTMLResponse(""), "尚未收到任何訊息", "err")
    return templates.TemplateResponse(request, "_notify_chats.html", {"request": request, "chats": chats})
