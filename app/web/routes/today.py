"""今天：三個時段的製作進度與需要處理的事項。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.markets import SLOT_KINDS
from app.models import CarouselPostPackage, ContentSlot, PostStatus
from app.web.ui import parse_iso, render

router = APIRouter()

STEPS = ["選題", "生成", "審稿", "Notion", "排程"]
_REVIEWED = {PostStatus.READY, PostStatus.SCHEDULED, PostStatus.PUBLISHING, PostStatus.PUBLISHED}
_GENERATED = _REVIEWED | {PostStatus.READY_TO_REVIEW}
_SCHEDULED = {PostStatus.SCHEDULED, PostStatus.PUBLISHING, PostStatus.PUBLISHED}
_FAILED = {PostStatus.QUALITY_FAILED, PostStatus.FAILED}


def step_states(post: CarouselPostPackage | None, notion_ok: bool) -> list[str]:
    """回傳每一步的狀態：d 完成、r 進行中（下一步）、f 失敗、空字串 尚未。"""
    if post is None:
        return [""] * len(STEPS)
    done = [True, post.status in _GENERATED, post.status in _REVIEWED, notion_ok, post.status in _SCHEDULED]
    states = ["d" if d else "" for d in done]
    if post.status in _FAILED:
        first_open = next((i for i, d in enumerate(done) if not d), 1)
        states[first_open] = "f"
    else:
        nxt = next((i for i, d in enumerate(done) if not d), None)
        if nxt is not None:
            states[nxt] = "r"
    return states


def status_text(post: CarouselPostPackage | None, notion_ok: bool) -> tuple[str, str, str]:
    """(文字, chip 樣式, 補充說明)"""
    if post is None:
        return "待選題", "", ""
    if post.status in _FAILED:
        return "品質檢查未通過" if post.status == PostStatus.QUALITY_FAILED else "失敗", "err", ""
    if post.status in _SCHEDULED:
        return "已排程" if post.status == PostStatus.SCHEDULED else "已發布", "ok", ""
    if notion_ok:
        return "已同步 Notion", "ok", ""
    if post.status in _REVIEWED:
        return "尚無 Notion 同步紀錄", "warn", ""
    if post.status == PostStatus.READY_TO_REVIEW:
        return "待審稿", "warn", ""
    return "製作中", "", ""


def _notion_synced(repo, account_id: str, post: CarouselPostPackage | None) -> bool:  # noqa: ANN001
    """最近一次成功同步必須晚於貼文最後一次更新（重新生成後就不算已同步）。"""
    if post is None:
        return False
    sync = repo.last_job_by_label(account_id=account_id, job="notion_sync", label=post.post_id)
    updated = repo.post_updated_at(post.post_id)
    if sync is None or not sync.get("finished_at"):
        return False
    return not updated or str(sync["finished_at"]) >= updated


@router.get("/", response_class=HTMLResponse)
def today(request: Request, date: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    settings = request.app.state.settings
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    tz = ZoneInfo(market.timezone)
    now = datetime.now(tz)
    try:
        target = datetime.strptime(date, "%Y-%m-%d").date() if date else (now + timedelta(days=1)).date()
    except ValueError:
        target = (now + timedelta(days=1)).date()
    date_str = target.isoformat()

    posts = repo.list_posts(account_id=active.account_id)
    by_slot: dict[ContentSlot, CarouselPostPackage] = {}
    for p in posts:
        if p.scheduled_at_taipei.startswith(date_str) and p.slot not in by_slot:
            by_slot[p.slot] = p

    rows = []
    for kind in SLOT_KINDS:
        post = by_slot.get(kind)
        notion_ok = _notion_synced(repo, active.account_id, post)
        text, kind_css, _ = status_text(post, notion_ok)
        rows.append(
            {
                "kind": kind,
                "time": market.time_for(kind),
                "track": market.track_name(kind),
                "post": post,
                "steps": step_states(post, notion_ok),
                "status": text,
                "status_kind": kind_css,
            }
        )

    attention: list[dict[str, str]] = []
    research = repo.last_job(account_id=active.account_id, job="research", only_ok=True)
    if research is None:
        attention.append({"level": "warn", "title": "還沒有研究資料", "body": "選題需要研究案例當依據。", "href": "/research", "cta": "開始研究"})
    else:
        finished = parse_iso(str(research["finished_at"]))
        if finished and (datetime.now(finished.tzinfo) - finished) > timedelta(hours=36):
            days = (datetime.now(finished.tzinfo) - finished).days
            attention.append({"level": "warn", "title": f"研究資料已 {max(days, 1)} 天沒更新", "body": "選題會用到舊資料。", "href": "/research", "cta": "重新研究"})
    notion = repo.get_notion_binding(active.account_id)
    if not (notion and notion.get("database_id")) and not settings.notion_token.strip():
        attention.append({"level": "warn", "title": "這個帳號還沒綁定 Notion", "body": "製作完成後無法同步到資料庫。", "href": "/settings#notion", "cta": "前往綁定"})
    elif notion and notion.get("schema_missing"):
        miss = "、".join(str(m) for m in notion["schema_missing"])
        attention.append({"level": "warn", "title": f"Notion 資料庫少了 {len(notion['schema_missing'])} 個欄位", "body": f"缺少：{miss}。寫入時這些欄位會被略過。", "href": "/settings#notion", "cta": "查看差異"})
    tiktok = repo.get_tiktok_binding(active.account_id)
    cap = str(tiktok["publish_capability"]) if tiktok else "export_only"
    if cap != "public":
        body = "排程貼文只會發為「僅自己可見」。" if cap == "self_only" else "目前只能匯出圖片與貼文文字，需自行在 TikTok App 發布。"
        attention.append({"level": "warn", "title": "TikTok 尚不能公開發布", "body": body, "href": "/settings#tiktok", "cta": "查看"})
    for r in rows:
        if r["post"] and r["post"].status in _FAILED:
            attention.append({"level": "err", "title": f"{r['time']} 這篇沒有通過品質檢查", "body": r["post"].zh_tw_title, "href": "/studio", "cta": "前往處理"})

    weekday = "一二三四五六日"[target.weekday()]
    return render(
        request,
        "today.html",
        "today",
        "今天",
        rows=rows,
        steps=STEPS,
        date_str=date_str,
        date_label=f"{target.month}/{target.day} 週{weekday}",
        attention=attention,
    )
