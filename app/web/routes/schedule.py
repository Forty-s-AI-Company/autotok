"""自動排程設定：每個帳號的開關、時間、下一個動作與最近結果。"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.scheduler import last_tick, next_action_text, plan, publish_mode
from app.web.ui import templates, toast

router = APIRouter()
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
RESULT_LABEL = {"ok": ("成功", "ok"), "error": ("失敗", "err"), "skipped": ("略過", ""), "missed": ("錯過", "err"), "running": ("進行中", "")}


@router.get("/schedule/panel", response_class=HTMLResponse)
def panel(request: Request) -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    now = datetime.now(UTC)
    actions = plan(repo, cfg, now)
    rows = []
    for acc in repo.list_accounts():
        market = repo.get_market_profile(acc.account_id)
        binding = repo.get_tiktok_binding(acc.account_id)
        cap = str(binding["publish_capability"]) if binding else "export_only"
        recent = []
        for r in repo.recent_jobs_like(account_id=acc.account_id, prefix="auto_", limit=12):
            if r["job"] == "auto_notify":
                continue
            label, kind = RESULT_LABEL.get(str(r["status"]), (str(r["status"]), ""))
            recent.append({"row": r, "label": label, "kind": kind, "text": str(r["summary"] or r["error"] or "")[:80]})
        rows.append(
            {
                "acc": acc,
                "market": market,
                "cap": cap,
                "next": next_action_text(actions, acc.account_id, now, market.timezone) if market.auto_enabled else "",
                "mode": publish_mode(repo, acc.account_id, market),
                "recent": recent[:5],
            }
        )
    tick = last_tick(repo)
    tz = rows[0]["market"].timezone if rows else "Asia/Taipei"
    return templates.TemplateResponse(request, "_schedule_panel.html", {"request": request, "rows": rows, "tick": tick, "tz": tz})


@router.post("/schedule/save")
async def save(request: Request) -> Response:
    repo = request.app.state.repo
    form = await request.form()
    account_id = str(form.get("account_id", ""))
    if repo.get_account(account_id) is None:
        return toast(Response(status_code=200), "找不到帳號", "err")
    market = repo.get_market_profile(account_id)
    for key, attr in (("research_time", "auto_research_time"), ("prepare_time", "auto_prepare_time")):
        value = str(form.get(key, "")).strip()
        if value:
            if not TIME_RE.match(value):
                return toast(Response(status_code=200), f"時間格式不正確：{value}（24 小時制 HH:MM）", "err")
            setattr(market, attr, value)
    market.auto_enabled = form.get("auto_enabled") is not None
    binding = repo.get_tiktok_binding(account_id)
    can_publish = bool(binding and binding["publish_capability"] == "public")
    market.auto_publish = form.get("auto_publish") is not None and can_publish
    repo.save_market_profile(account_id, market)
    return toast(Response(status_code=200), "已儲存自動排程設定", events=["scheduleChanged"])
