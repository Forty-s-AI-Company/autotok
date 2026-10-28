"""製作：選題 → 文案 → 生圖 → 品質檢查 → 審稿 → 同步 Notion。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.editorial import (
    EditorialDraft,
    TopicBrief,
    editorial_status,
)
from app.engines import codex_status
from app.markets import SLOT_KINDS
from app.media import verify_carousel_directory
from app.models import CarouselPostPackage, ContentSlot, PostStatus
from app.notion_sync import NotionSyncService
from app.pipeline import make_post_id, produce_post
from app.production import (
    verify_visual_diversity_and_text_integrity,
)
from app.web.jobs import JobBusyError, JobHandle
from app.web.routes.library import STATUS_LABEL, media_urls
from app.web.ui import render, toast

router = APIRouter()
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LOCKED_STATUSES = {PostStatus.SCHEDULED, PostStatus.PUBLISHING, PostStatus.PUBLISHED, PostStatus.SENT_TO_INBOX}

ENGINES = {
    "codex": "Codex 生圖＋即時文案",
    "procedural": "快速渲染＋即時文案（圖片為示意）",
    "sample": "範例文案＋快速渲染（模擬）",
}
COUNT_CHOICES = ["auto", *[str(n) for n in range(3, 11)]]


def _parse_slot(value: str) -> ContentSlot:
    try:
        return ContentSlot(value)
    except ValueError:
        return ContentSlot.NOON_1130


def _load_draft(post: CarouselPostPackage) -> EditorialDraft | None:
    path = post.media_dir.parent / "draft.json"
    if not path.is_file():
        return None
    try:
        return EditorialDraft.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def qa_rows(post: CarouselPostPackage) -> list[dict[str, object]]:
    """現場重跑品質檢查，逐項列出通過與否與原因。"""
    rows: list[dict[str, object]] = []
    base = verify_carousel_directory(post.media_dir, raise_on_error=False, expected_count=post.slide_count)
    if base.errors:
        rows.extend({"ok": False, "text": e} for e in base.errors)
    else:
        rows.append({"ok": True, "text": f"{post.slide_count} 張皆為 1080×1350 JPEG，頁碼連續、沒有重複圖片"})
    _, div_errors = verify_visual_diversity_and_text_integrity(post, post.media_dir)
    if div_errors:
        rows.extend({"ok": False, "text": e} for e in div_errors)
    else:
        rows.append({"ok": True, "text": "文案沒有違禁承諾，各頁內容不重複，圖片視覺有差異"})
    return rows


def slide_payload(post: CarouselPostPackage, artifacts_root: Path) -> list[dict[str, str]]:
    target = media_urls(post.media_dir, artifacts_root)
    zh = media_urls(post.media_dir.parent / "zh_tw", artifacts_root)
    out = []
    for i, s in enumerate(post.slides):
        head, _, body = s.vi_text.partition(" — ")
        zh_head, sep, zh_body = s.zh_tw_translation.partition("：")
        out.append(
            {
                "target_img": target[i] if i < len(target) else "",
                "zh_img": zh[i] if i < len(zh) else "",
                "target_title": head,
                "target_body": body,
                "zh_title": zh_head if sep else "",
                "zh_body": zh_body if sep else s.zh_tw_translation,
            }
        )
    return out


@router.get("/studio", response_class=HTMLResponse)
def studio(request: Request, topic: str = "", date: str = "", slot: str = "", post: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    root: Path = request.app.state.project_root
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    posts = repo.list_posts(account_id=active.account_id)

    selected = next((p for p in posts if p.post_id == post), None) if post else None
    if selected is None and post == "" and not (topic or date or slot) and posts:
        selected = posts[0]

    now = datetime.now(ZoneInfo(market.timezone))
    default_date = (now + timedelta(days=1)).date().isoformat()
    topic_obj = repo.get_topic_opportunity(topic) if topic else None
    chosen_slot = _parse_slot(slot) if slot else (topic_obj.recommended_slot if topic_obj else ContentSlot.NOON_1130)
    if selected is not None:
        date_val, slot_val = selected.scheduled_at_taipei[:10], selected.slot
    else:
        date_val, slot_val = (date or default_date), chosen_slot

    ed_ok, ed_text = editorial_status(cfg)
    engine = codex_status(cfg.codex_bin)
    review = None
    if selected is not None:
        draft = _load_draft(selected)
        label, kind = STATUS_LABEL.get(selected.status, (selected.status.value, ""))
        rows = qa_rows(selected)
        review = {
            "post": selected,
            "status_label": label,
            "status_kind": kind,
            "qa": rows,
            "qa_failed": [r for r in rows if not r["ok"]],
            "risk_flags": draft.risk_flags if draft else [],
            "slides_data": slide_payload(selected, root / "artifacts"),
        }
    return render(
        request,
        "studio.html",
        "studio",
        "製作",
        review=review,
        recent=posts[:8],
        topic=topic_obj,
        date_val=date_val,
        slot_val=slot_val,
        slot_kinds=SLOT_KINDS,
        count_choices=COUNT_CHOICES,
        engines=ENGINES,
        editorial_ok=ed_ok,
        editorial_text=ed_text,
        codex_ok=engine.ok,
    )


def _produce_job(
    app,  # noqa: ANN001
    *,
    account_id: str,
    date_str: str,
    slot: ContentSlot,
    engine: str,
    count: int | None,
    brief: TopicBrief,
):
    def run(handle: JobHandle) -> str:
        pkg = produce_post(
            app.state.repo,
            app.state.settings,
            app.state.project_root,
            account_id=account_id,
            date_str=date_str,
            slot=slot,
            engine=engine,
            count=count,
            brief=brief,
            progress=handle.progress,
            editorial_provider=getattr(app.state, "editorial_provider", None),
        )
        return f"{pkg.post_id}：{pkg.slide_count} 張，已通過品質檢查"

    return run


@router.post("/studio/produce")
def produce(
    request: Request,
    date: str = Form(...),
    slot: str = Form(...),
    engine: str = Form("codex"),
    count: str = Form("auto"),
    topic_id: str = Form(""),
    topic_title: str = Form(""),
    points: str = Form(""),
    angle: str = Form(""),
) -> Response:
    app = request.app
    repo = app.state.repo
    cfg = app.state.settings
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    resp = Response(status_code=200)

    if not DATE_RE.match(date):
        return toast(resp, "日期格式不正確（YYYY-MM-DD）", "err")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return toast(resp, "日期格式不正確（YYYY-MM-DD）", "err")
    if engine not in ENGINES:
        return toast(resp, "不認識的引擎選項", "err")
    if count not in COUNT_CHOICES:
        return toast(resp, "張數只能是「依內容決定」或 3–10", "err")
    slot_kind = _parse_slot(slot)

    brief = TopicBrief(title=topic_title.strip(), angle=angle.strip(), points=[p.strip() for p in points.splitlines() if p.strip()])
    if topic_id:
        topic = repo.get_topic_opportunity(topic_id)
        if topic is None:
            return toast(resp, "找不到這個選題，可能已被移除", "err")
        brief.title = brief.title or topic.title_zh_tw
        brief.angle = brief.angle or topic.angle
        cases = {c.case_id: c for c in repo.list_research_cases(days=None)}
        notes = [f"- {cases[c].creator}（{cases[c].narrative_category}）：{cases[c].hook_notes[:140]}" for c in topic.research_case_ids if c in cases]
        brief.source_summary = "\n".join(notes[:5])
    if engine != "sample" and not (brief.title or brief.points):
        return toast(resp, "請填寫主題或重點，或從「研究」選一個題目送過來", "err")

    if engine != "sample" and getattr(app.state, "editorial_provider", None) is None:
        ok, why = editorial_status(cfg)
        if not ok:
            return toast(resp, why, "err")
    if engine == "codex" and not codex_status(cfg.codex_bin).ok:
        return toast(resp, "找不到 Codex CLI，無法用 Codex 生圖。可改選「快速渲染」。", "err")

    existing_id = make_post_id(active.account_id, date, slot_kind)
    existing_status = repo.get_post_status(existing_id)
    if existing_status in LOCKED_STATUSES:
        return toast(resp, "這篇已經排程或發布，不能重新生成。要做新的內容請改選其他日期或時段。", "err")

    label_date = f"{int(date[5:7])}/{int(date[8:10])}"
    try:
        app.state.jobs.submit(
            account_id=active.account_id,
            job="produce",
            label=f"製作 {label_date} {market.time_for(slot_kind)}",
            fn=_produce_job(
                app,
                account_id=active.account_id,
                date_str=date,
                slot=slot_kind,
                engine=engine,
                count=None if count == "auto" else int(count),
                brief=brief,
            ),
        )
    except JobBusyError as exc:
        return toast(resp, str(exc), "err")
    return toast(resp, "已開始製作，完成後頁面會自動更新")


@router.post("/studio/sync")
def sync_notion(request: Request, post_id: str = Form(...)) -> Response:
    app = request.app
    repo = app.state.repo
    cfg = app.state.settings
    active = repo.get_active_account()
    resp = Response(status_code=200)
    post = next((p for p in repo.list_posts(account_id=active.account_id) if p.post_id == post_id), None)
    if post is None:
        return toast(resp, "找不到這篇貼文", "err")
    if post.status == PostStatus.PUBLISHING:
        return toast(resp, "這篇正在發布中，稍後再同步", "err")
    if any(not r["ok"] for r in qa_rows(post)):
        return toast(resp, "這篇還沒通過品質檢查，不能同步到 Notion", "err")
    factory = getattr(NotionSyncService, "for_account", None)
    service = factory(repo, cfg, active.account_id) if factory else None
    if service is None:
        return toast(resp, "尚未綁定 Notion。到「設定」登入並選擇資料庫後再同步。", "err")

    zh_dir = post.media_dir.parent / "zh_tw"

    def run(handle: JobHandle) -> str:
        handle.progress("上傳圖片並建立頁面")
        result = service.sync_post_to_notion(post, zh_media_dir=zh_dir)
        return f"已同步到 Notion：{result.notion_page_url}"

    try:
        app.state.jobs.submit(account_id=active.account_id, job="notion_sync", label=post.post_id, fn=run)
    except JobBusyError as exc:
        return toast(resp, str(exc), "err")
    return toast(resp, "已開始同步 Notion")
