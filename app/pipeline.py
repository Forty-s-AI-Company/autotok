"""製作與同步流程（不依賴 Web），網頁操作台與排程器共用。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.config import AppSettings
from app.editorial import (
    EditorialDraft,
    EditorialProvider,
    EditorialRequest,
    TopicBrief,
    get_editorial_provider,
)
from app.errors import AutotokSystemError, MediaValidationError
from app.fit import CaseFit
from app.markets import MarketProfile
from app.models import CarouselPostPackage, ContentSlot, PostStatus, TopicCandidateStatus, TopicOpportunity
from app.notion_sync import NotionSyncService
from app.production import ChatGPTCodexCliCarouselGenerator, produce_carousel_with_qa_retry
from app.research_service import CASE_WINDOW_DAYS, refit_account, topic_from_case
from app.state import PostStateRepository

Progress = Callable[[str], None]
MIN_AUTO_FIT = 40.0


def make_post_id(account_id: str, date_str: str, slot: ContentSlot, default_account_id: str = "carry-vietnam-tw") -> str:
    tag = slot.value.split("_")[0].replace(":", "")
    if account_id == default_account_id:
        return f"post-{date_str}-{tag}"
    return f"post-{account_id}-{date_str}-{tag}"


def draft_path(project_root: Path, date_str: str, post_id: str) -> Path:
    return project_root / "artifacts" / "daily_production" / date_str / post_id / "draft.json"


def brief_from_topic(repo: PostStateRepository, topic: TopicOpportunity) -> TopicBrief:
    brief = TopicBrief(title=topic.title_zh_tw, angle=topic.angle)
    cases = {c.case_id: c for c in repo.list_research_cases(days=None)}
    notes = [
        f"- {cases[c].creator}（{cases[c].narrative_category}）：{cases[c].hook_notes[:140]}"
        for c in topic.research_case_ids
        if c in cases
    ]
    brief.source_summary = "\n".join(notes[:5])
    return brief


def produce_post(
    repo: PostStateRepository,
    cfg: AppSettings,
    root: Path,
    *,
    account_id: str,
    date_str: str,
    slot: ContentSlot,
    engine: str,
    count: int | None,
    brief: TopicBrief,
    progress: Progress = lambda msg: None,
    editorial_provider: EditorialProvider | None = None,
) -> CarouselPostPackage:
    """文案 → 生圖 → 品質檢查 → 存貼文。engine：codex / procedural / sample。"""
    account = repo.get_account(account_id)
    if account is None:
        raise AutotokSystemError("找不到帳號")
    market = repo.get_market_profile(account_id)
    post_id = make_post_id(account_id, date_str, slot)
    out_base = root / "artifacts" / "daily_production" / date_str
    draft: EditorialDraft | None = None
    if engine == "sample":
        draft_path(root, date_str, post_id).unlink(missing_ok=True)
    else:
        progress("產生文案")
        provider = editorial_provider or get_editorial_provider(cfg, workdir=root)
        draft = provider.generate(
            EditorialRequest(
                topic=brief,
                market=market,
                track_name=market.track_name(slot),
                account_handle=account.handle,
                brand_label=account.display_name,
                slide_count=count,
            )
        )
        draft_file = draft_path(root, date_str, post_id)
        draft_file.parent.mkdir(parents=True, exist_ok=True)
        draft_file.write_text(draft.model_dump_json(indent=2), encoding="utf-8")
    progress(f"製作 {count or '自動'} 張圖片並檢查品質")
    codex = ChatGPTCodexCliCarouselGenerator(model=cfg.codex_model, sandbox=cfg.codex_image_sandbox) if engine == "codex" else None
    custom = "\n".join([brief.title, *brief.points]) if engine == "sample" and brief.title else ""
    try:
        pkg, _, _ = produce_carousel_with_qa_retry(
            slot=slot,
            post_id=post_id,
            scheduled_date_str=date_str,
            output_base_dir=out_base,
            account_id=account_id,
            account_handle=account.handle,
            custom_content=custom,
            target_slide_count=count,
            use_codex_cli=engine == "codex",
            codex_generator=codex,
            draft=draft,
            market=market if draft else None,
            brand_label=account.display_name if draft else None,
        )
    except MediaValidationError as exc:
        failed = getattr(exc, "package", None)
        if failed is not None:
            repo.upsert_post(failed)
        raise
    except Exception as exc:
        # 舊圖可能已被清掉：貼文不能繼續顯示為可發布
        if repo.get_post_status(post_id) is not None:
            repo.update_status(post_id, PostStatus.FAILED, last_error=f"重新生成失敗：{exc}")
        raise
    if engine == "sample":
        pkg.is_mock = True
    repo.upsert_post(pkg)
    return pkg


def sync_post_to_notion(
    repo: PostStateRepository,
    cfg: AppSettings,
    account_id: str,
    post: CarouselPostPackage,
    progress: Progress = lambda msg: None,
) -> str:
    service = NotionSyncService.for_account(repo, cfg, account_id)
    if service is None:
        raise AutotokSystemError("尚未綁定 Notion")
    progress("上傳圖片並建立頁面")
    result = service.sync_post_to_notion(post, zh_media_dir=post.media_dir.parent / "zh_tw")
    return f"已同步到 Notion：{result.notion_page_url}"


def pick_topic(
    repo: PostStateRepository,
    account_id: str,
    market: MarketProfile,
    slot: ContentSlot,
    *,
    min_fit: float = MIN_AUTO_FIT,
) -> tuple[TopicOpportunity | None, str]:
    """為指定內容線挑一個題目：真實來源案例、適配度夠、近 90 天沒用過；優先配對建議時段。"""
    cases = [c for c in repo.list_research_cases(days=CASE_WINDOW_DAYS) if not c.is_mock and c.evidence_level.value != "mock_fixture"]
    if not cases:
        return None, "沒有可用的研究案例"
    stored = repo.list_case_fits(account_id)
    if len(stored) < len(cases):
        refit_account(repo, account_id, market)
        stored = repo.list_case_fits(account_id)
    ranked: list[tuple[int, float, object]] = []
    for c in cases:
        row = stored.get(c.case_id)
        if row is None or float(row["score"]) < min_fit:  # type: ignore[arg-type]
            continue
        match = 0 if str(row["recommended_slot"]) == slot.value else 1
        ranked.append((match, -float(row["score"]), c))  # type: ignore[arg-type]
    ranked.sort(key=lambda t: (t[0], t[1]))
    if not ranked:
        return None, f"沒有適配度達 {int(min_fit)} 分的案例"
    reasons: list[str] = []
    for _, _, case in ranked:
        existing = repo.get_topic_opportunity(f"topic-{account_id}-{case.case_id}")  # type: ignore[attr-defined]
        if existing is not None and existing.status == TopicCandidateStatus.APPROVED:
            continue
        row = stored[case.case_id]  # type: ignore[attr-defined]
        fit = CaseFit(
            case_id=case.case_id,  # type: ignore[attr-defined]
            score=float(row["score"]),
            reason=str(row["reason"]),
            flag=str(row["flag"]),
            recommended_slot=ContentSlot(str(row["recommended_slot"])) if row["recommended_slot"] else slot,
            parts=_parts(case, market),
        )
        topic, why = topic_from_case(repo, account_id=account_id, market=market, case=case, fit=fit)  # type: ignore[arg-type]
        if topic is not None:
            return topic, ""
        reasons.append(why)
    return None, reasons[0] if reasons else "案例都已使用過"


def _parts(case, market: MarketProfile) -> dict[str, float]:  # noqa: ANN001
    from app.fit import score_case

    return score_case(case, market).parts


def mark_topic_used(repo: PostStateRepository, account_id: str, topic: TopicOpportunity) -> None:
    topic.status = TopicCandidateStatus.APPROVED
    repo.upsert_topic_opportunity(topic, account_id=account_id)
