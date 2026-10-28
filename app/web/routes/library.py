"""內容庫：已製作的貼文，依狀態篩選，卡片預設只顯示縮圖列。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.adapters import get_uploader_adapter
from app.errors import AutotokSystemError
from app.models import CarouselPostPackage, PostStatus
from app.web.ui import render, toast

router = APIRouter()

STATUS_LABEL = {
    PostStatus.DRAFT: ("備稿", ""),
    PostStatus.RESEARCHED: ("備稿", ""),
    PostStatus.GENERATING: ("生成中", ""),
    PostStatus.READY_TO_REVIEW: ("待審稿", "warn"),
    PostStatus.QUALITY_FAILED: ("品質未通過", "err"),
    PostStatus.READY: ("可發布", "ok"),
    PostStatus.SCHEDULED: ("已排程", "ok"),
    PostStatus.PUBLISHING: ("發布中", ""),
    PostStatus.PUBLISHED: ("已發布", "ok"),
    PostStatus.SENT_TO_INBOX: ("已傳到收件匣，待你在 App 發布", "warn"),
    PostStatus.FAILED: ("發布失敗", "err"),
    PostStatus.UNKNOWN: ("狀態未知", "warn"),
    PostStatus.BLOCKED_NEEDS_AUTH: ("需要授權", "warn"),
}


def media_urls(directory: Path, artifacts_root: Path) -> list[str]:
    """把資料夾內的 slide_*.jpg 轉成 /media 網址；不在 artifacts 底下就略過。"""
    try:
        rel = directory.resolve().relative_to(artifacts_root.resolve())
    except (ValueError, OSError):
        return []
    if not directory.is_dir():
        return []
    return [f"/media/{rel.as_posix()}/{f.name}" for f in sorted(directory.glob("slide_*.jpg"))]


def post_view(p: CarouselPostPackage, artifacts_root: Path) -> dict[str, object]:
    label, kind = STATUS_LABEL.get(p.status, (p.status.value, ""))
    zh_dir = p.media_dir.parent / "zh_tw"
    return {
        "post": p,
        "status_label": label,
        "status_kind": kind,
        "target_thumbs": media_urls(p.media_dir, artifacts_root),
        "zh_thumbs": media_urls(zh_dir, artifacts_root),
    }


@router.get("/library", response_class=HTMLResponse)
def library(request: Request, status: str = "") -> HTMLResponse:
    repo = request.app.state.repo
    active = repo.get_active_account()
    artifacts_root = request.app.state.project_root / "artifacts"
    posts = repo.list_posts(account_id=active.account_id)
    groups = {"待處理": {PostStatus.READY_TO_REVIEW, PostStatus.QUALITY_FAILED, PostStatus.DRAFT, PostStatus.RESEARCHED, PostStatus.GENERATING, PostStatus.UNKNOWN, PostStatus.BLOCKED_NEEDS_AUTH}, "可發布": {PostStatus.READY, PostStatus.SCHEDULED, PostStatus.PUBLISHING}, "已發布": {PostStatus.PUBLISHED, PostStatus.SENT_TO_INBOX}, "失敗": {PostStatus.FAILED}}
    counts = {name: sum(1 for p in posts if p.status in members) for name, members in groups.items()}
    if status in groups:
        posts = [p for p in posts if p.status in groups[status]]
    views = [post_view(p, artifacts_root) for p in posts]
    return render(request, "library.html", "library", "內容庫", views=views, counts=counts, status=status, total=len(repo.list_posts(account_id=active.account_id)))


@router.post("/library/export")
def export_post(request: Request, post_id: str = Form(...)) -> Response:
    """匯出發布包（圖片、貼文文字、清單）。不會對 TikTok 發出任何請求。"""
    repo = request.app.state.repo
    active = repo.get_active_account()
    pkg = next((p for p in repo.list_posts(account_id=active.account_id) if p.post_id == post_id), None)
    if pkg is None:
        return toast(Response(status_code=200), "找不到這篇貼文", "err")
    try:
        plan = get_uploader_adapter("manual_export", request.app.state.settings).publish_carousel(pkg, dry_run=False)
    except AutotokSystemError as exc:
        return toast(Response(status_code=200), f"匯出失敗：{exc}", "err")
    return toast(Response(status_code=200), f"已匯出到 {plan.endpoint_or_target}")
