"""Notion 綁定：連線（OAuth 或 Token）、選擇資料庫／上層頁面、欄位檢查與新增欄位。"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.errors import AutotokSystemError, BlockedNeedsAuthError
from app.integrations.notion import binding as nb
from app.integrations.notion import oauth
from app.integrations.notion.binding import find_database_owner
from app.integrations.notion.client import NotionClient, validate_notion_id
from app.integrations.notion.schema_check import SchemaCheck, check_schema
from app.web.ui import page_context, templates, toast

router = APIRouter()

LIST_TTL_SECONDS = 60
AUTH_LABEL = {"oauth": "Notion 登入", "token": "貼上的 Token", "global": "全域 Token（.env）"}


def _transport(request: Request) -> httpx.BaseTransport | None:
    """測試可在 app.state.notion_transport 注入 MockTransport。"""
    return getattr(request.app.state, "notion_transport", None)


def _cache(request: Request) -> dict[str, tuple[float, dict[str, Any]]]:
    if not hasattr(request.app.state, "notion_cache"):
        request.app.state.notion_cache = {}
    return request.app.state.notion_cache


def _flash(request: Request) -> dict[str, dict[str, str]]:
    if not hasattr(request.app.state, "notion_flash"):
        request.app.state.notion_flash = {}
    return request.app.state.notion_flash


def _pending(request: Request) -> dict[str, dict[str, str]]:
    """建立到一半（頁面已建立、資料庫失敗）的紀錄，避免重複建立空頁面。"""
    if not hasattr(request.app.state, "notion_pending"):
        request.app.state.notion_pending = {}
    return request.app.state.notion_pending


def _changed(resp: Response) -> Response:
    """保留 toast 的 HX-Trigger，並加上 notionChanged。"""
    import json

    existing = resp.headers.get("HX-Trigger")
    payload: dict[str, Any] = json.loads(existing) if existing else {}
    payload["notionChanged"] = True
    resp.headers["HX-Trigger"] = json.dumps(payload)
    return resp


def _reply(msg: str, kind: str = "ok", *, changed: bool = True) -> Response:
    resp = toast(Response(status_code=200), msg, kind)
    return _changed(resp) if changed else resp


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _client(request: Request, account_id: str) -> NotionClient | None:
    found = nb.resolve_token(request.app.state.repo, request.app.state.settings, account_id)
    if found is None:
        return None
    return NotionClient(found[0], transport=_transport(request))


def _run_check(request: Request, account_id: str, client: NotionClient, database_id: str) -> tuple[SchemaCheck, dict[str, Any]]:
    db = client.get_database(database_id)
    check = check_schema(db.get("properties", {}))
    request.app.state.repo.save_notion_binding(
        account_id,
        schema_missing=check.stored_names(),
        schema_checked_at=_now(),
        verified_at=_now(),
    )
    return check, db


def _listing(request: Request, account_id: str, client: NotionClient, *, force: bool = False) -> dict[str, Any]:
    cache = _cache(request)
    hit = cache.get(account_id)
    if hit and not force and time.monotonic() - hit[0] < LIST_TTL_SECONDS:
        return hit[1]
    data: dict[str, Any] = {"databases": [], "pages": [], "error": "", "fetched_at": _now()}
    try:
        data["databases"] = client.search("database")
        data["pages"] = client.search("page")
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        data["error"] = str(exc)
    if not data["error"]:
        cache[account_id] = (time.monotonic(), data)
    return data


@router.get("/notion/panel", response_class=HTMLResponse)
def notion_panel(request: Request, account_id: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    settings = request.app.state.settings
    account = repo.get_account(account_id) or repo.get_active_account()
    account_id = account.account_id
    binding = repo.get_notion_binding(account_id)
    found = nb.resolve_token(repo, settings, account_id)
    auth_key = "global" if found and found[1] == "global" else str((binding or {}).get("auth_method") or "token")

    listing: dict[str, Any] = {"databases": [], "pages": [], "error": "", "fetched_at": None}
    schema: SchemaCheck | None = None
    if found:
        client = NotionClient(found[0], transport=_transport(request))
        listing = _listing(request, account_id, client)
        db_id = str(binding["database_id"]) if binding and binding.get("database_id") else ""
        if not db_id and found[1] == "global":
            db_id = settings.notion_database_id.strip()
        if db_id and not listing["error"]:
            try:
                schema = check_schema(client.get_database(db_id).get("properties", {}))
            except (AutotokSystemError, BlockedNeedsAuthError) as exc:
                listing = {**listing, "error": str(exc)}

    flash = _flash(request).pop(account_id, None)
    ctx = page_context(request, "settings", "設定")
    ctx.update(
        account=account,
        binding=binding,
        connected=found is not None,
        auth_label=AUTH_LABEL.get(auth_key, auth_key),
        using_global=bool(found and found[1] == "global"),
        listing=listing,
        schema=schema,
        bound_db_id=str(binding["database_id"]) if binding and binding.get("database_id") else "",
        oauth_enabled=oauth.oauth_enabled(settings),
        redirect_uri=settings.notion_redirect_uri,
        flash=flash,
        pending=_pending(request).get(account_id),
        default_db_name=f"AutoTok｜{account.handle}",
        has_global_token=bool(settings.notion_token.strip()) and account_id == "carry-vietnam-tw",
    )
    return templates.TemplateResponse(request, "_notion_panel.html", ctx)


@router.post("/notion/connect-token")
def connect_token(request: Request, account_id: str = Form(...), token: str = Form(...)) -> Response:
    repo = request.app.state.repo
    if repo.get_account(account_id) is None:
        return _reply("找不到帳號", "err", changed=False)
    token = token.strip()
    if not token:
        return _reply("請貼上 Notion 整合 Token", "err", changed=False)
    try:
        who = NotionClient(token, transport=_transport(request)).whoami()
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        return _reply(str(exc), "err", changed=False)
    nb.store_connection(repo, account_id, token=token, auth_method="token", workspace_name=who["workspace_name"])
    _cache(request).pop(account_id, None)
    return _reply(f"已連線到 {who['workspace_name'] or 'Notion'}")


@router.get("/notion/oauth/start")
def oauth_start(request: Request, account_id: str = "") -> Response:
    settings = request.app.state.settings
    if request.app.state.repo.get_account(account_id) is None:
        return Response("找不到帳號", status_code=404)
    if not oauth.oauth_enabled(settings):
        return Response("尚未設定 NOTION_CLIENT_ID / NOTION_CLIENT_SECRET", status_code=400)
    return RedirectResponse(oauth.build_authorize_url(settings, oauth.new_state(account_id)), status_code=303)


@router.get("/notion/oauth/callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    repo = request.app.state.repo
    account_id = oauth.pop_state(state) if state else None
    if account_id is None:
        return Response("授權狀態無效或已過期，請回到設定頁重新操作。", status_code=400)
    back = RedirectResponse(f"/settings?account={account_id}#notion", status_code=303)
    if error or not code:
        _flash(request)[account_id] = {"kind": "err", "msg": "Notion 授權已取消或失敗。"}
        return back
    try:
        data = oauth.exchange_code(request.app.state.settings, code, transport=_transport(request))
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        _flash(request)[account_id] = {"kind": "err", "msg": str(exc)}
        return back
    nb.store_connection(
        repo, account_id, token=str(data["access_token"]), auth_method="oauth", workspace_name=str(data.get("workspace_name") or "")
    )
    _cache(request).pop(account_id, None)
    _flash(request)[account_id] = {"kind": "ok", "msg": f"已連線到 {data.get('workspace_name') or 'Notion'}"}
    return back


@router.post("/notion/disconnect")
def disconnect(request: Request, account_id: str = Form(...)) -> Response:
    nb.clear_connection(request.app.state.repo, account_id)
    _cache(request).pop(account_id, None)
    return _reply("已中斷 Notion 連線")


@router.post("/notion/refresh")
def refresh(request: Request, account_id: str = Form(...)) -> Response:
    client = _client(request, account_id)
    if client is None:
        return _reply("尚未連線 Notion", "err", changed=False)
    data = _listing(request, account_id, client, force=True)
    if data["error"]:
        return _reply(data["error"], "err", changed=False)
    request.app.state.repo.save_notion_binding(account_id, verified_at=_now())
    return _reply(f"找到 {len(data['databases'])} 個資料庫、{len(data['pages'])} 個頁面")


@router.post("/notion/bind-database")
def bind_database(
    request: Request, account_id: str = Form(...), database_id: str = Form(...), database_title: str = Form("")
) -> Response:
    repo = request.app.state.repo
    try:
        database_id = validate_notion_id(database_id)
    except AutotokSystemError as exc:
        return _reply(str(exc), "err", changed=False)
    owner = find_database_owner(repo, request.app.state.settings, database_id, account_id)
    if owner:
        return _reply(f"這個資料庫已經綁定給 {owner}。兩個帳號共用同一個資料庫會互相覆蓋頁面，請為每個帳號各自準備資料庫。", "err", changed=False)
    client = _client(request, account_id)
    if client is None:
        return _reply("尚未連線 Notion", "err", changed=False)
    try:
        check, db = _run_check(request, account_id, client, database_id)
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        return _reply(str(exc), "err", changed=False)
    title = database_title or "".join(str(p.get("plain_text", "")) for p in db.get("title", []))
    repo.save_notion_binding(account_id, database_id=database_id, database_title=title)
    if check.ok:
        return _reply(f"已綁定「{title}」，欄位全部相符")
    return _reply(f"已綁定「{title}」，有 {len(check.missing) + len(check.type_mismatch)} 個欄位需要處理", "warn")


@router.post("/notion/bind-parent")
def bind_parent(request: Request, account_id: str = Form(...), page_id: str = Form(...)) -> Response:
    try:
        page_id = validate_notion_id(page_id)
    except AutotokSystemError as exc:
        return _reply(str(exc), "err", changed=False)
    request.app.state.repo.save_notion_binding(account_id, parent_page_id=page_id)
    return _reply("已設定上層頁面")


@router.post("/notion/recheck")
def recheck(request: Request, account_id: str = Form(...)) -> Response:
    repo = request.app.state.repo
    binding = repo.get_notion_binding(account_id)
    client = _client(request, account_id)
    if client is None or not binding or not binding.get("database_id"):
        return _reply("請先連線並選擇資料庫", "err", changed=False)
    try:
        check, _ = _run_check(request, account_id, client, str(binding["database_id"]))
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        return _reply(str(exc), "err", changed=False)
    return _reply("欄位全部相符" if check.ok else "已重新檢查，仍有欄位需要處理", "ok" if check.ok else "warn")


@router.post("/notion/add-fields")
def add_fields(request: Request, account_id: str = Form(...)) -> Response:
    """只新增缺少且 API 可建立的欄位；既有欄位與資料完全不動。"""
    repo = request.app.state.repo
    binding = repo.get_notion_binding(account_id)
    client = _client(request, account_id)
    if client is None or not binding or not binding.get("database_id"):
        return _reply("請先連線並選擇資料庫", "err", changed=False)
    database_id = str(binding["database_id"])
    try:
        check = check_schema(client.get_database(database_id).get("properties", {}))
        todo = {m.name: m.type for m in check.creatable_missing}
        if not todo:
            return _reply("沒有可以自動新增的欄位", "warn", changed=False)
        client.add_properties(database_id, todo)
        check, _ = _run_check(request, account_id, client, database_id)
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        return _reply(str(exc), "err", changed=False)
    return _reply(f"已新增 {len(todo)} 個欄位" + ("" if check.ok else "，其餘需在 Notion 內手動建立"))


@router.get("/notion/parent-pages", response_class=HTMLResponse)
def parent_pages(request: Request, account_id: str = "", q: str = "", parent_page_id: str = "") -> HTMLResponse:
    """自動建立資料庫用的「放在哪個頁面底下」清單；`q` 有值時直接向 Notion 搜尋。"""
    error = ""
    pages: list[dict[str, str]] = []
    client = _client(request, account_id)
    if client is None:
        error = "尚未連線 Notion"
    else:
        try:
            pages = client.search("page", q.strip()) if q.strip() else _listing(request, account_id, client)["pages"]
        except (AutotokSystemError, BlockedNeedsAuthError) as exc:
            error = str(exc)
    ctx = page_context(request, "settings", "設定")
    ctx.update(pages=pages, error=error, selected_parent=parent_page_id, query=q)
    return templates.TemplateResponse(request, "_notion_parents.html", ctx)


@router.post("/notion/create-database")
def create_database(
    request: Request,
    account_id: str = Form(...),
    parent_page_id: str = Form(...),
    name: str = Form(""),
    replace: str = Form(""),
) -> Response:
    """在所選頁面底下新增 1 個頁面與 1 個資料庫，成功後自動綁定。不修改、不刪除任何既有內容。"""
    repo = request.app.state.repo
    account = repo.get_account(account_id)
    if account is None:
        return _reply("找不到帳號", "err", changed=False)
    try:
        parent_page_id = validate_notion_id(parent_page_id)
    except AutotokSystemError as exc:
        return _reply(str(exc), "err", changed=False)
    binding = repo.get_notion_binding(account_id)
    if binding and binding.get("database_id") and replace != "1":
        return _reply(
            f"已經綁定「{binding.get('database_title') or '資料庫'}」，不會重複建立。若要改綁到新的資料庫，請勾選「改綁到新建立的資料庫」。",
            "err",
            changed=False,
        )
    client = _client(request, account_id)
    if client is None:
        return _reply("尚未連線 Notion", "err", changed=False)
    name = (name.strip() or f"AutoTok｜{account.handle}")[:100]

    pending = _pending(request)
    page = pending.get(account_id)
    if page and not (page.get("parent_id") == parent_page_id and page.get("name") == name):
        page = None
    if page is None:
        try:
            created = client.create_page(parent_page_id, name)
        except (AutotokSystemError, BlockedNeedsAuthError) as exc:
            return _reply(f"建立頁面失敗，沒有新增任何內容：{exc}", "err", changed=False)
        page = {**created, "parent_id": parent_page_id, "name": name}
        pending[account_id] = page

    try:
        db = client.create_database(page["id"], name)
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        return _reply(
            f"頁面「{name}」已建立（{page['url'] or page['id']}），但建立資料庫失敗：{exc} 再按一次「建立頁面與資料庫」會沿用這個頁面，不會重複建立。",
            "err",
        )

    pending.pop(account_id, None)
    _cache(request).pop(account_id, None)
    repo.save_notion_binding(account_id, database_id=validate_notion_id(db["id"]), database_title=name, parent_page_id=parent_page_id)
    try:
        check, _ = _run_check(request, account_id, client, db["id"])
    except (AutotokSystemError, BlockedNeedsAuthError) as exc:
        _flash(request)[account_id] = {"kind": "warn", "msg": f"已建立並綁定「{name}」，但欄位檢查失敗：{exc}", "url": db["url"]}
        return _reply(f"已建立並綁定「{name}」", "warn")
    msg = f"已建立並綁定「{name}」" + ("" if check.ok else "，有欄位需要處理")
    _flash(request)[account_id] = {"kind": "ok" if check.ok else "warn", "msg": msg, "url": db["url"]}
    return _reply(msg)
