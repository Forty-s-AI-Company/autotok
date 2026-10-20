"""TikTok 授權（Login Kit）與發布能力偵測。尚未用真實帳號驗證，見 app/integrations/tiktok.py。"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import credentials
from app.errors import AutotokSystemError
from app.integrations.tiktok import OAuthStates, TikTokClient, TikTokTokens, capability_from_privacy_options
from app.publisher import image_host_from_settings
from app.web.ui import templates, toast

router = APIRouter()

CAP_LABEL = {"public": "可公開發布", "self_only": "僅自己可見", "export_only": "僅匯出"}


def _states(request: Request) -> OAuthStates:
    st = request.app.state
    if not hasattr(st, "tiktok_states"):
        st.tiktok_states = OAuthStates()
    return st.tiktok_states


def _client(request: Request) -> TikTokClient:
    return TikTokClient(request.app.state.settings, transport=getattr(request.app.state, "tiktok_transport", None))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _save(request: Request, account_id: str, tokens: TikTokTokens) -> tuple[str, str]:
    """用 token 查詢 creator info，寫入綁定；回傳 (capability, 授權帳號名稱)。"""
    repo = request.app.state.repo
    info = _client(request).creator_info(tokens.access_token)
    options = [str(o) for o in info.get("privacy_level_options", []) if isinstance(o, str)]  # type: ignore[union-attr]
    cap = capability_from_privacy_options(options)
    if cap == "public" and not request.app.state.settings.tiktok_app_audited:
        cap = "self_only"  # App 尚未通過官方審核時，不顯示可公開發布
    username = str(info.get("creator_username", "")).strip()
    acc = repo.get_account(account_id)
    if not username:
        raise AutotokSystemError("TikTok 沒有回傳帳號名稱，無法確認授權的是哪個帳號。", code="TIKTOK_NO_USERNAME")
    if acc and username.lower() != acc.handle.lstrip("@").lower():
        raise AutotokSystemError(
            f"授權的 TikTok 帳號是 @{username}，與這個帳號（{acc.handle}）不同。請先在 TikTok 切換到正確的帳號再授權。",
            code="TIKTOK_ACCOUNT_MISMATCH",
        )
    ref = f"tiktok:{account_id}"
    credentials.set_secret(ref, tokens.dumps())
    repo.save_tiktok_binding(
        account_id,
        token_ref=ref,
        publish_capability=cap,
        expires_at=_iso(tokens.expires_at),
        verified_at=datetime.now(UTC).isoformat(),
    )
    return cap, username


@router.get("/tiktok/panel", response_class=HTMLResponse)
def panel(request: Request, account_id: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    acc = repo.get_account(account_id) or repo.get_active_account()
    market = repo.get_market_profile(acc.account_id)
    binding = repo.get_tiktok_binding(acc.account_id)
    cap = str(binding["publish_capability"]) if binding else "export_only"
    return templates.TemplateResponse(
        request,
        "_tiktok_panel.html",
        {
            "request": request,
            "account": acc,
            "tz": market.timezone,
            "binding": binding,
            "connected": bool(binding and binding.get("token_ref")),
            "cap": cap,
            "cap_label": CAP_LABEL.get(cap, cap),
            "app_ready": bool(cfg.tiktok_client_key.strip() and cfg.tiktok_client_secret.strip()),
            "redirect_uri": cfg.tiktok_redirect_uri,
            "live_on": cfg.allow_live_publish,
            "audited": cfg.tiktok_app_audited,
            "host_ready": image_host_from_settings(cfg) is not None,
        },
    )


@router.post("/tiktok/connect")
def connect(request: Request, account_id: str = Form(...)) -> Response:
    if request.app.state.repo.get_account(account_id) is None:
        return toast(Response(status_code=200), "找不到帳號", "err")
    try:
        state = _states(request).issue(account_id)
        url = _client(request).build_authorize_url(state)
    except AutotokSystemError as exc:
        return toast(Response(status_code=200), str(exc), "err")
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = url
    return resp


@router.get("/tiktok/oauth/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = "") -> Response:
    account_id = _states(request).consume(state) if state else None
    if account_id is None:
        return RedirectResponse("/settings?tiktok=state_invalid#tiktok", status_code=303)
    if error or not code:
        return RedirectResponse("/settings?tiktok=denied#tiktok", status_code=303)
    try:
        tokens = _client(request).exchange_code(code)
        _save(request, account_id, tokens)
    except AutotokSystemError:
        return RedirectResponse(f"/settings?account={account_id}&tiktok=failed#tiktok", status_code=303)
    return RedirectResponse(f"/settings?account={account_id}&tiktok=ok#tiktok", status_code=303)


@router.post("/tiktok/refresh")
def refresh(request: Request, account_id: str = Form(...)) -> Response:
    raw = credentials.get_secret(f"tiktok:{account_id}")
    tokens = TikTokTokens.loads(raw) if raw else None
    if tokens is None:
        return toast(Response(status_code=200), "還沒有授權", "err")
    try:
        if tokens.expires_at - time.time() < 300 and tokens.refresh_token:
            tokens = _client(request).refresh(tokens.refresh_token)
            credentials.set_secret(f"tiktok:{account_id}", tokens.dumps())  # refresh token 已旋轉，先存起來
        cap, _ = _save(request, account_id, tokens)
    except AutotokSystemError as exc:
        return toast(Response(status_code=200), str(exc), "err")
    return toast(Response(status_code=200), f"已重新檢查：{CAP_LABEL[cap]}", events=["tiktokChanged"])


@router.post("/tiktok/disconnect")
def disconnect(request: Request, account_id: str = Form(...)) -> Response:
    credentials.delete_secret(f"tiktok:{account_id}")
    request.app.state.repo.save_tiktok_binding(account_id, token_ref="", publish_capability="export_only")
    return toast(Response(status_code=200), "已中斷 TikTok 授權", events=["tiktokChanged"])
