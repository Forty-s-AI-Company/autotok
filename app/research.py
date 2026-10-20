"""市場研究（Market Research Agent）與成長策略選題評分（Growth Strategy Agent）核心模組。

遵循 MASTER_SPEC.md 第 4, 5, 7, 10 節規範：
1. 完整內建 12 組原始越南市場搜尋關鍵字種子（5.2 節），並支援研究擴充、意圖、時效性與中文翻譯記錄。
2. 嚴格處理指標缺失與口徑（5.4 節）：
   - 完整資料：互動率 = (讚 + 留言 + 分享) / 觀看 -> `EngagementRateType.FULL`
   - 缺失讚/留言/分享任一項但有觀看數：改算「部分互動率」並明確列出已計入與缺失欄位 -> `EngagementRateType.PARTIAL`
   - 缺失觀看數或全部互動指標：`EngagementRateType.UNAVAILABLE`（回傳 `None`，嚴禁預設填 `0`）
   - 新增追蹤轉換率 = 可合理歸因新增追蹤 / 觀看；無歸因資料時一律回傳 `None`，不得宣稱已確認。
3. 選題研究性評分（5.4 節）：
   - 在台受眾相關性 35% + 追蹤動機 25% + 分享收藏 20% + 輪播適配 10% + 資料可信度 10%。
   - 明確標註「此為可調內部假設，不是官方演算法分數」。
4. 90 天內來源 URL 與主題名稱去重（Editorial / Growth 查重保護）。
5. `ResearchProvider` 介面：
   - 支援讀取公開可驗證或使用者匯入之真實 JSON/JSONL 案例檔（30–50 篇批次研究，不足時如實回報數量，絕不灌水）。
   - 支援 `MockResearchProvider` 供本機測試與 CI 驗證，且明確標記 `is_mock=True` 與 `evidence_level="mock_fixture"`。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.models import (
    ContentSlot,
    EngagementMetricsResult,
    EngagementRateType,
    EvidenceLevel,
    ResearchCase,
    SeedKeywordEntry,
    SourceType,
    TopicCandidateStatus,
    TopicOpportunity,
    TopicScoreBreakdown,
)

# MASTER_SPEC.md 第 5.2 節定義之 12 組原始種子關鍵字
INITIAL_SEED_KEYWORDS: list[SeedKeywordEntry] = [
    SeedKeywordEntry(
        keyword_vi="người Việt ở Đài Loan",
        translation_zh_tw="在台越南人",
        intent="社群認同與在台生活綜合資訊",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="người Việt tại Đài Loan",
        translation_zh_tw="在台灣的越南人",
        intent="在台越南族群日常經驗與互助",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="cuộc sống ở Đài Loan",
        translation_zh_tw="台灣生活",
        intent="日常食衣住行、交通與生活適應指南",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="cộng đồng người Việt Đài Loan",
        translation_zh_tw="在台越南人社群",
        intent="同鄉聚會、活動與支援網絡",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.MORNING_0700,
    ),
    SeedKeywordEntry(
        keyword_vi="làm việc tại Đài Loan",
        translation_zh_tw="在台工作",
        intent="職場環境、合法工作權益與個人成長（不虛構職缺或簽證）",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.MORNING_0700,
    ),
    SeedKeywordEntry(
        keyword_vi="lương ở Đài Loan",
        translation_zh_tw="台灣薪資",
        intent="公開基本工資法規知識與生活開支規劃（不誇大收入）",
        timeliness="seasonal",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.MORNING_0700,
    ),
    SeedKeywordEntry(
        keyword_vi="xuất khẩu lao động Đài Loan",
        translation_zh_tw="赴台海外勞務",
        intent="赴台前準備、防詐騙提醒與過來人心情調適",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.MORNING_0700,
    ),
    SeedKeywordEntry(
        keyword_vi="việc làm thêm ở Đài Loan",
        translation_zh_tw="台灣兼職",
        intent="留學生合法工作證規定與數位技能培養（嚴守在台合法工作限制）",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.EVENING_1800,
    ),
    SeedKeywordEntry(
        keyword_vi="ẩm thực Đài Loan",
        translation_zh_tw="台灣美食",
        intent="夜市小吃、越南人合口味的台灣料理推薦",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="du lịch Đài Loan",
        translation_zh_tw="台灣旅行",
        intent="假日搭捷運/火車一日遊景點與拍照打卡攻略",
        timeliness="seasonal",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="khác biệt Việt Nam Đài Loan",
        translation_zh_tw="台越差異",
        intent="台越文化、飲食、垃圾車、超商服務等有趣對比",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.NOON_1130,
    ),
    SeedKeywordEntry(
        keyword_vi="người Việt nhớ nhà",
        translation_zh_tw="越南人思鄉",
        intent="異鄉打拼情緒共鳴、家人牽掛與自我鼓勵",
        timeliness="evergreen",
        source="master_spec_5.2",
        recommended_slot=ContentSlot.MORNING_0700,
    ),
]


def compute_engagement_metrics(case: ResearchCase) -> EngagementMetricsResult:
    """根據 MASTER_SPEC.md 第 5.4 節計算互動率與新增追蹤轉換率。

    - 嚴禁將缺失指標（`None`）當成 `0` 混入完整互動率比較。
    - 當 `views`, `likes`, `comments`, `shares` 皆非 `None` 且 `views > 0` 時，回傳 `FULL`。
    - 當 `views > 0` 且 `likes`, `comments`, `shares` 至少有一項非 `None` 但有部分缺失時，
      回傳 `PARTIAL`（部分互動率）並明確標註已包含與缺失的欄位，禁止與 `FULL` 直接跨口徑比較。
    - 當 `views` 為 `None` 或 `0`，或三項互動指標全部為 `None` 時，回傳 `UNAVAILABLE` (`rate_value=None`)。
    """
    metric_map = {
        "likes": case.likes,
        "comments": case.comments,
        "shares": case.shares,
    }
    present = [k for k, v in metric_map.items() if v is not None]
    missing = [k for k, v in metric_map.items() if v is None]

    # 計算新增追蹤轉換率（無歸因資料時必為 None）
    follower_conv: float | None = None
    if (
        case.views is not None
        and case.views > 0
        and case.attributed_new_followers is not None
    ):
        follower_conv = round(case.attributed_new_followers / case.views, 6)

    if case.views is None or case.views <= 0 or not present:
        return EngagementMetricsResult(
            rate_type=EngagementRateType.UNAVAILABLE,
            rate_value=None,
            present_numerator_fields=present,
            missing_numerator_fields=missing,
            follower_conversion_rate=follower_conv,
            note="無有效觀看數或互動指標全數缺失，無法計算互動率（缺失欄位不預設為 0）。",
        )

    numerator = sum(metric_map[k] or 0 for k in present)
    rate = round(numerator / case.views, 6)

    if not missing:
        return EngagementMetricsResult(
            rate_type=EngagementRateType.FULL,
            rate_value=rate,
            present_numerator_fields=present,
            missing_numerator_fields=[],
            follower_conversion_rate=follower_conv,
            note="完整互動率 = (讚 + 留言 + 分享) / 觀看。",
        )

    return EngagementMetricsResult(
        rate_type=EngagementRateType.PARTIAL,
        rate_value=rate,
        present_numerator_fields=present,
        missing_numerator_fields=missing,
        follower_conversion_rate=follower_conv,
        note=(
            f"部分互動率（僅含 {', '.join(present)}；缺失 {', '.join(missing)}）/"
            " 觀看。注意：不可與完整互動率跨口徑直接比較。"
        ),
    )


def compute_topic_score(
    *,
    taiwan_audience_relevance: float,
    follow_motivation: float,
    share_save_motivation: float,
    carousel_adaptability: float,
    data_credibility: float,
) -> TopicScoreBreakdown:
    """依據 MASTER_SPEC.md 第 5.4 節計算選題研究性評分（權重 35% / 25% / 20% / 10% / 10%）。"""
    weighted = (
        taiwan_audience_relevance * 0.35
        + follow_motivation * 0.25
        + share_save_motivation * 0.20
        + carousel_adaptability * 0.10
        + data_credibility * 0.10
    )
    return TopicScoreBreakdown(
        taiwan_audience_relevance=round(taiwan_audience_relevance, 2),
        follow_motivation=round(follow_motivation, 2),
        share_save_motivation=round(share_save_motivation, 2),
        carousel_adaptability=round(carousel_adaptability, 2),
        data_credibility=round(data_credibility, 2),
        weighted_total=round(weighted, 2),
    )


def normalize_topic_text(text: str) -> str:
    """正規化主題文字以供 90 天去重比對（轉小寫、移除標點與多餘空白）。"""
    cleaned = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(cleaned.split())


def check_90_day_dedup(
    *,
    candidate_title_vi: str,
    candidate_urls: list[str],
    existing_topics_within_90d: list[tuple[str, str]],
    existing_case_urls_within_90d: set[str],
) -> tuple[bool, str]:
    """檢查候選主題與來源 URL 是否在過去 90 天內重複。

    參數：
    - `candidate_title_vi`：候選越南文主題名稱
    - `candidate_urls`：候選主題引用的來源 URL 清單
    - `existing_topics_within_90d`：近 90 天內已存在之 `(topic_id, title_vi)` 清單
    - `existing_case_urls_within_90d`：近 90 天內已被既有主題採用過的來源 URL 集合
    """
    for url in candidate_urls:
        if url in existing_case_urls_within_90d:
            return False, f"來源 URL '{url}' 於近 90 天內已被其他選題使用過"

    norm_candidate = normalize_topic_text(candidate_title_vi)
    candidate_tokens = set(norm_candidate.split())

    for existing_id, existing_title in existing_topics_within_90d:
        norm_existing = normalize_topic_text(existing_title)
        if not norm_existing:
            continue
        if norm_candidate == norm_existing:
            return False, f"主題名稱與近 90 天內既有主題 '{existing_id}' 完全重複"

        existing_tokens = set(norm_existing.split())
        if candidate_tokens and existing_tokens:
            overlap = len(candidate_tokens & existing_tokens) / max(
                len(candidate_tokens), len(existing_tokens)
            )
            if overlap >= 0.7:
                return (
                    False,
                    f"主題名稱與近 90 天內既有主題 '{existing_id}' 高度重疊 (相似度 {overlap:.0%})",
                )

    return True, "通過 90 天主題與 URL 去重檢查"


class ResearchProvider(ABC):
    """市場研究資料提供者抽象介面（MASTER_SPEC.md 第 10 節）。"""

    provider_name: str

    @abstractmethod
    def collect_cases(self, *, max_cases: int = 50) -> list[ResearchCase]:
        """蒐集或載入研究案例清單（目標 30–50 篇；若真實來源不足則如實回傳實際數量，絕不編造）。"""


class JsonFileResearchProvider(ResearchProvider):
    """從使用者或公開擷取工具匯出之本地 JSON / JSONL 檔案載入真實研究案例。

    若檔案中案例不足 30 篇，如實回傳實際篇數並在報表中標註不足，絕不自動塞入假案例。
    """

    provider_name = "json_file"

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path.expanduser().resolve()

    def collect_cases(self, *, max_cases: int = 50) -> list[ResearchCase]:
        if not self.file_path.is_file():
            raise FileNotFoundError(f"找不到研究案例資料檔: {self.file_path}")

        raw_text = self.file_path.read_text(encoding="utf-8").strip()
        if not raw_text:
            return []

        items: list[dict[str, object]] = []
        if self.file_path.suffix.lower() == ".jsonl":
            for line in raw_text.splitlines():
                if line.strip():
                    items.append(json.loads(line))
        else:
            parsed = json.loads(raw_text)
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, dict) and "cases" in parsed and isinstance(parsed["cases"], list):
                items = parsed["cases"]

        cases: list[ResearchCase] = []
        for item in items[:max_cases]:
            cases.append(ResearchCase.model_validate(item))
        return cases


class MockResearchProvider(ResearchProvider):
    """供離線開發與自動化測試使用的 Mock 研究案例產生器。

    所有產出案例強制標記 `is_mock=True` 與 `evidence_level=EvidenceLevel.MOCK_FIXTURE`，
    並涵蓋：
    - 完整公開指標之自然流量樣本 (`SourceType.ORGANIC`)
    - 部分指標缺失（如無分享數、無粉絲數）之樣本（測試部分互動率口徑）
    - Top Ads 廣告樣本 (`SourceType.AD`，測試廣告與自然流量分離）
    """

    provider_name = "mock"

    def collect_cases(self, *, max_cases: int = 35) -> list[ResearchCase]:
        now = datetime.now(UTC)
        templates = [
            (
                "cuộc sống ở Đài Loan",
                "6 điều bỡ ngỡ khi mới sang Đài Loan làm việc và học tập",
                "清單式開場：第一頁直接列出 6 個台灣生活習慣差異（垃圾車音樂、悠遊卡、發票兌獎）",
                "practical_checklist",
                SourceType.ORGANIC,
                True,
            ),
            (
                "ẩm thực Đài Loan",
                "Top 6 món ăn chợ đêm Đài Loan hợp khẩu vị người Việt nhất",
                "視覺美食鉤子：第一張高清夜市小吃特寫，標註價格與越文名稱對照",
                "food_guide",
                SourceType.ORGANIC,
                True,
            ),
            (
                "người Việt nhớ nhà",
                "Những buổi chiều tan ca ở Đài Loan chỉ muốn gọi về cho mẹ",
                "情緒共鳴鉤子：傍晚下班街景搭配思鄉獨白，引發在台移工與留學生留言分享",
                "emotional_story",
                SourceType.ORGANIC,
                False,  # 模擬缺失 shares 與 follower_count
            ),
            (
                "khác biệt Việt Nam Đài Loan",
                "6 điểm khác biệt thú vị giữa cửa hàng tiện lợi Đài Loan và Việt Nam",
                "對比型鉤子：台灣超商萬能服務（寄件、繳費、買車票）6 頁圖解",
                "comparison_carousel",
                SourceType.ORGANIC,
                True,
            ),
            (
                "du lịch Đài Loan",
                "Lịch trình 1 ngày đi tàu điện MRT khám phá Đài Bắc cuối tuần",
                "實用收藏型：週末放假不花大錢的捷運一日遊 6 站路線圖",
                "travel_itinerary",
                SourceType.ORGANIC,
                False,
            ),
            (
                "làm việc tại Đài Loan",
                "[AD SAMPLE] Quảng cáo khóa học kỹ năng số và quay dựng bằng điện thoại",
                "Top Ads 廣告樣本：手機拍攝與剪輯教學廣告（單獨標註為廣告，不混入自然互動率排行）",
                "ad_creative",
                SourceType.AD,
                True,
            ),
        ]

        cases: list[ResearchCase] = []
        count = min(max_cases, 35)
        for idx in range(1, count + 1):
            tpl = templates[(idx - 1) % len(templates)]
            kw, title_hint, hook, category, src_type, has_full_metrics = tpl
            pub_date = (now - timedelta(days=(idx % 25) + 1)).isoformat()

            views = 15000 + (idx * 1250)
            likes = 950 + (idx * 65)
            comments = 85 + (idx * 7)
            shares = (140 + (idx * 12)) if has_full_metrics else None
            followers = (12000 + idx * 300) if has_full_metrics else None
            missing = [] if has_full_metrics else ["shares", "follower_count", "attributed_new_followers"]
            if has_full_metrics:
                missing = ["attributed_new_followers"]

            cases.append(
                ResearchCase(
                    case_id=f"mock-case-{idx:03d}",
                    source_url=f"https://www.tiktok.com/@mock_vn_tw_creator_{idx % 8}/video/740000000000000{idx:04d}",
                    creator=f"@mock_vn_tw_creator_{idx % 8}",
                    market="VN_IN_TW",
                    source_type=src_type,
                    published_at=pub_date,
                    collected_at=now.isoformat(),
                    views=views,
                    likes=likes,
                    comments=comments,
                    shares=shares,
                    follower_count=followers,
                    attributed_new_followers=None,
                    missing_fields=missing,
                    hook_notes=f"[MOCK 樣本｜關鍵字: {kw}] {title_hint} — {hook}",
                    narrative_category=category,
                    evidence_level=EvidenceLevel.MOCK_FIXTURE,
                    confidence=0.65 if src_type == SourceType.AD else (0.85 if has_full_metrics else 0.70),
                    is_mock=True,
                )
            )
        return cases


def build_topic_opportunities_from_cases(
    cases: list[ResearchCase],
    *,
    existing_topics_within_90d: list[tuple[str, str]] | None = None,
    existing_case_urls_within_90d: set[str] | None = None,
) -> list[TopicOpportunity]:
    """Growth Strategy Agent：從研究案例分群比較（排除廣告樣本混同自然流量）並產出候選主題機會。"""
    existing_topics = list(existing_topics_within_90d or [])
    existing_urls = set(existing_case_urls_within_90d or set())

    # 自然流量案例與廣告案例分離，不將 Top Ads 直接混同自然爆款結構
    organic_cases = [c for c in cases if c.source_type == SourceType.ORGANIC]
    if not organic_cases:
        return []

    # 依每日三時段（07:00 情緒/家庭/成長、11:30 台灣生活/美食/文化、18:00 手機創作/數位成長）建立候選主題模板
    candidate_specs = [
        {
            "topic_id": "topic-0700-homesick-growth",
            "title_vi": "6 cách giữ tinh thần tích cực và kết nối gia đình khi làm việc xa nhà tại Đài Loan",
            "title_zh_tw": "在台異鄉打拼必看：保持正向心態與家人溫暖聯繫的 6 個方法",
            "angle": "從下班後的思鄉情緒切入，提供具體可執行的跨國聯繫、生活節奏與合法社群互助建議（07:00 晨間陪伴時段）",
            "slot": ContentSlot.MORNING_0700,
            "category_filter": {"emotional_story", "practical_checklist"},
            "scores": (92.0, 86.0, 88.0, 90.0),
            "hypothesis": "晨間 07:00 發布結合思鄉共鳴與具體生活安定方法的 6 頁輪播，能提升在台越南受眾的主頁造訪與追蹤動機。",
        },
        {
            "topic_id": "topic-1130-taiwan-convenience-life",
            "title_vi": "6 tiện ích tại cửa hàng tiện lợi và chợ đêm Đài Loan giúp người Việt tiết kiệm thời gian",
            "title_zh_tw": "台灣超商與夜市 6 大實用生活技巧：幫在台越南朋友省時又省錢",
            "angle": "將台灣超商多功能服務與在地生活小常識拆解為 1 頁 1 重點的清晰圖卡（11:30 午間生活美食時段）",
            "slot": ContentSlot.NOON_1130,
            "category_filter": {"comparison_carousel", "food_guide", "travel_itinerary"},
            "scores": (95.0, 84.0, 94.0, 95.0),
            "hypothesis": "午間 11:30 發布高實用性的台灣生活與美食 6 頁清單，能顯著提升收藏率（Saves）與同鄉轉發分享率（Shares）。",
        },
        {
            "topic_id": "topic-1800-mobile-creator-skills",
            "title_vi": "6 bước chụp ảnh và làm bài đăng 6 trang bằng điện thoại ghi lại cuộc sống ở Đài Loan",
            "title_zh_tw": "用手機記錄台灣生活：6 步驟拍出高質感六頁圖文並建立個人數位影響力",
            "angle": "分享如何用手機實拍台灣日常、構圖與整理越南文筆記（18:00 晚間數位成長與創作時段）",
            "slot": ContentSlot.EVENING_1800,
            "category_filter": {"practical_checklist", "comparison_carousel"},
            "scores": (88.0, 90.0, 85.0, 92.0),
            "hypothesis": "晚間 18:00 發布手機拍攝與圖文整理技巧，能吸引對自我提升與創作感興趣的在台越南青年互動追蹤。",
        },
    ]

    now_iso = datetime.now(UTC).isoformat()
    opportunities: list[TopicOpportunity] = []

    for spec in candidate_specs:
        matched_cases = [
            c
            for c in organic_cases
            if c.narrative_category in spec["category_filter"]
            and c.source_url not in existing_urls
        ][:5]
        if not matched_cases:
            matched_cases = [c for c in organic_cases if c.source_url not in existing_urls][:3]
        if not matched_cases:
            matched_cases = organic_cases[:3]

        case_ids = [c.case_id for c in matched_cases]
        case_urls = [c.source_url for c in matched_cases]
        avg_confidence_pct = round(
            (sum(c.confidence for c in matched_cases) / len(matched_cases)) * 100.0, 2
        )

        rel, fol, shr, car = spec["scores"]
        score_breakdown = compute_topic_score(
            taiwan_audience_relevance=rel,
            follow_motivation=fol,
            share_save_motivation=shr,
            carousel_adaptability=car,
            data_credibility=avg_confidence_pct,
        )

        passed, reason = check_90_day_dedup(
            candidate_title_vi=str(spec["title_vi"]),
            candidate_urls=case_urls,
            existing_topics_within_90d=existing_topics,
            existing_case_urls_within_90d=existing_urls,
        )

        opp = TopicOpportunity(
            topic_id=str(spec["topic_id"]),
            title_vi=str(spec["title_vi"]),
            title_zh_tw=str(spec["title_zh_tw"]),
            angle=str(spec["angle"]),
            recommended_slot=spec["slot"],
            research_case_ids=case_ids,
            score_breakdown=score_breakdown,
            dedup_90d_passed=passed,
            dedup_reason=reason,
            experiment_hypothesis=str(spec["hypothesis"]),
            status=TopicCandidateStatus.CANDIDATE if passed else TopicCandidateStatus.DUPLICATE_REJECTED,
            created_at=now_iso,
        )
        opportunities.append(opp)

        if passed:
            existing_topics.append((opp.topic_id, opp.title_vi))
            existing_urls.update(case_urls)

    return opportunities
