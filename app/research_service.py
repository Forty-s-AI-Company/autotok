"""研究流程：蒐集 → 存檔 → 計算適配度；以及把案例轉成選題。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.fit import CaseFit, compute_fits
from app.markets import MarketProfile
from app.models import (
    ContentSlot,
    EngagementRateType,
    ResearchCase,
    TopicCandidateStatus,
    TopicOpportunity,
)
from app.research import check_90_day_dedup, compute_engagement_metrics, compute_topic_score
from app.research_providers import SourceProvider
from app.state import PostStateRepository

CASE_WINDOW_DAYS = 90


def refit_account(repo: PostStateRepository, account_id: str, market: MarketProfile) -> list[CaseFit]:
    """重新計算並寫入某帳號對所有近期案例的適配度。"""
    fits = compute_fits(repo.list_research_cases(days=CASE_WINDOW_DAYS), market)
    repo.replace_case_fits(account_id, [f.as_row() for f in fits])
    return fits


def missing_metric_count(cases: list[ResearchCase]) -> int:
    return sum(1 for c in cases if c.views is None or c.likes is None or c.comments is None or c.shares is None)


def run_research(
    repo: PostStateRepository,
    *,
    account_id: str,
    market: MarketProfile,
    provider: SourceProvider,
    max_cases: int,
    progress=lambda msg: None,  # noqa: ANN001
) -> str:
    """執行一次研究並回傳摘要。Provider 不可用或取不到資料時丟出例外，不退回假資料。"""
    avail = provider.availability()
    if not avail.ok:
        raise RuntimeError(avail.reason)
    progress(f"正在向「{provider.label}」蒐集案例…")
    cases = provider.collect_cases(max_cases=max_cases)
    if not cases:
        raise RuntimeError(f"「{provider.label}」沒有找到任何帶有來源網址的案例，沒有寫入任何資料。")
    progress(f"取得 {len(cases)} 筆，寫入並計算適配度…")
    for c in cases:
        repo.upsert_research_case(c)
    refit_account(repo, account_id, market)
    missing = missing_metric_count(cases)
    return f"{provider.label}：{len(cases)} 筆，{missing} 筆缺少互動數據"


def format_rate(case: ResearchCase) -> tuple[str, str]:
    """回傳 (顯示文字, 缺失說明)。缺值顯示「—」，部分口徑標「部分」。"""
    m = compute_engagement_metrics(case)
    zh = {"likes": "讚", "comments": "留言", "shares": "分享"}
    if m.rate_type == EngagementRateType.UNAVAILABLE or m.rate_value is None:
        return "—", ""
    pct = f"{m.rate_value * 100:.1f}%"
    if m.rate_type == EngagementRateType.PARTIAL:
        return f"部分 {pct}", "缺：" + "、".join(zh[k] for k in m.missing_numerator_fields)
    return pct, ""


def topic_from_case(
    repo: PostStateRepository,
    *,
    account_id: str,
    market: MarketProfile,
    case: ResearchCase,
    fit: CaseFit,
) -> tuple[TopicOpportunity | None, str]:
    """把案例轉成候選主題。沒有標題可用或 90 天內重複時回傳 (None, 原因)。"""
    title = case.title.strip() or case.title_zh.strip()
    if not title:
        return None, "這個案例沒有標題，無法建立主題。請先在匯入資料補上標題。"
    existing_topics, used_urls = repo.get_recent_topics_and_urls(days=CASE_WINDOW_DAYS, account_id=account_id)
    topic_id = f"topic-{account_id}-{case.case_id}"
    if topic_id in {t for t, _ in existing_topics}:
        reason = "已建立過的主題，沿用同一筆"
    else:
        passed, reason = check_90_day_dedup(
            candidate_title_vi=title,
            candidate_urls=[case.source_url],
            existing_topics_within_90d=existing_topics,
            existing_case_urls_within_90d=used_urls,
        )
        if not passed:
            return None, f"近 90 天已做過類似主題：{reason}"

    p = fit.parts
    relevance = (p.get("language", 0) + p.get("keyword", 0) + p.get("region", 0)) / 65 * 100
    share_save = p.get("engagement", 0) / 20 * 100
    score = compute_topic_score(
        taiwan_audience_relevance=min(100.0, relevance),
        follow_motivation=min(100.0, share_save),
        share_save_motivation=min(100.0, share_save),
        carousel_adaptability=min(100.0, p.get("format", 0) / 15 * 100),
        data_credibility=case.confidence * 100,
    )
    track = market.track_name(fit.recommended_slot)
    topic = TopicOpportunity(
        topic_id=topic_id,
        title_vi=title,
        title_zh_tw=case.title_zh.strip() or title,
        target_audience=market.audience or "未指定",
        angle=(case.hook_notes.strip() or f"參考案例的{case.narrative_category}結構，改寫成原創內容") + f"（來源：{case.source_url}）",
        recommended_slot=fit.recommended_slot if isinstance(fit.recommended_slot, ContentSlot) else ContentSlot(fit.recommended_slot),
        research_case_ids=[case.case_id],
        score_breakdown=score,
        dedup_90d_passed=True,
        dedup_reason=reason,
        experiment_hypothesis=f"以此案例的{case.narrative_category}結構製作，驗證在「{track}」時段是否提升收藏與追蹤。",
        status=TopicCandidateStatus.CANDIDATE,
        created_at=datetime.now(UTC).isoformat(),
    )
    repo.upsert_topic_opportunity(topic, account_id=account_id)
    return topic, ""
