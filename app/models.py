"""系統核心資料模型與狀態機定義。

遵循 MASTER_SPEC.md 第 7 與第 8 節規範，支援完整生命週期狀態機與未來擴充（Research / Topic / Post）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 輪播張數規則（依內容切分後決定張數，不再固定 6 張）：
# - 最少 3 張：封面 1 張 + 至少 1 個重點 + 結尾 CTA 1 張
# - 預設上限 10 張：兼顧閱讀完成率與收藏意願（TikTok 照片貼文官方上限為 35 張）
MIN_SLIDE_COUNT = 3
MAX_SLIDE_COUNT = 10
TIKTOK_PHOTO_HARD_LIMIT = 35
DEFAULT_ACCOUNT_ID = "carry-vietnam-tw"


class TikTokAccountProfile(BaseModel):
    """多帳號管理設定檔（支援單一使用者管理並切換多個 TikTok 帳號）。"""

    account_id: str = Field(min_length=1, description="帳號唯一識別碼（如 carry-vietnam-tw）")
    handle: str = Field(min_length=2, description="TikTok 帳號名稱（如 @carry.vietnam.tw）")
    display_name: str = Field(min_length=1, description="帳號顯示名稱與品牌副標")
    market_focus: str = Field(default="在台越南人生活與成長", description="目標受眾與市場定位")
    primary_language: str = Field(default="zh-TW", description="創作者審稿與主要檢視語言（預設繁體中文）")
    target_language: str = Field(default="vi", description="發布目標受眾語言（如 vi 越南文）")
    notion_database_id: str = Field(default="f1bc34396f154e62bca90c9deea19d7a", description="綁定之 Notion 資料庫 ID")
    is_active: bool = Field(default=False, description="是否為目前作用中的切換帳號")


class PostStatus(StrEnum):
    """貼文狀態機列舉（對應 MASTER_SPEC.md 第 8 節）。"""

    DRAFT = "DRAFT"
    RESEARCHED = "RESEARCHED"
    GENERATING = "GENERATING"
    READY_TO_REVIEW = "READY_TO_REVIEW"
    QUALITY_FAILED = "QUALITY_FAILED"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    SENT_TO_INBOX = "SENT_TO_INBOX"  # 已傳到 TikTok 收件匣草稿，等使用者在 App 內按下發布
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    BLOCKED_NEEDS_AUTH = "BLOCKED_NEEDS_AUTH"


class ContentSlot(StrEnum):
    """每日固定三時段與對應主題分類（Asia/Taipei）。"""

    MORNING_0700 = "07:00_EMOTION_GROWTH"
    NOON_1130 = "11:30_TAIWAN_LIFE_CULTURE"
    EVENING_1800 = "18:00_CREATOR_DIGITAL_GROWTH"


class SourceType(StrEnum):
    """研究案例來源類型（嚴格區分自然流量與廣告樣本，不得混同）。"""

    ORGANIC = "organic"
    AD = "ad"


class EvidenceLevel(StrEnum):
    """研究案例可信度與證據等級（MASTER_SPEC.md 第 4, 5, 10 節）。"""

    VERIFIED_PUBLIC = "verified_public"
    PARTIAL_PUBLIC = "partial_public"
    AD_LIBRARY = "ad_library"
    UNVERIFIED = "unverified"
    MANUAL_IMPORT = "manual_import"
    MOCK_FIXTURE = "mock_fixture"


class EngagementRateType(StrEnum):
    """互動率計算口徑（缺失指標時改用部分互動率並明確標註，禁止跨口徑直接比較）。"""

    FULL = "full_engagement_rate"
    PARTIAL = "partial_engagement_rate"
    UNAVAILABLE = "unavailable"


class TopicCandidateStatus(StrEnum):
    """主題機會候選狀態。"""

    CANDIDATE = "CANDIDATE"
    APPROVED = "APPROVED"
    DUPLICATE_REJECTED = "DUPLICATE_REJECTED"
    ARCHIVED = "ARCHIVED"


class SeedKeywordEntry(BaseModel):
    """越南市場搜尋字典條目（MASTER_SPEC.md 第 5.2 節）。"""

    keyword_vi: str = Field(min_length=1, description="越南文搜尋關鍵字")
    translation_zh_tw: str = Field(min_length=1, description="繁體中文翻譯")
    intent: str = Field(min_length=1, description="搜尋意圖分類（如生活適應、工作薪資、思鄉情緒、美食旅行）")
    timeliness: str = Field(default="evergreen", description="時效性（evergreen / seasonal / trending）")
    source: str = Field(default="master_spec_seed", description="關鍵字來源（seed / expanded_from_research）")
    recommended_slot: ContentSlot = Field(default=ContentSlot.NOON_1130, description="建議對應之發布時段")


class ResearchCase(BaseModel):
    """市場研究案例模型（對應 MASTER_SPEC.md 第 7 節 `research_cases`）。

    嚴格規範：
    - 缺失指標必須保留為 `None`，嚴禁預設填 `0`（MASTER_SPEC.md 第 4 節）。
    - 若為 `SourceType.AD`（如 Top Ads），必須單獨標註廣告樣本，不得混同自然流量。
    - 無法實際觀看影片時，不得捏造開場或留言，必須標記 `evidence_level` 與 `missing_fields`。
    """

    case_id: str = Field(description="研究案例唯一識別碼")
    source_url: str = Field(min_length=1, description="唯一來源 URL")
    title: str = Field(default="", description="來源原標題（來源語言）")
    title_zh: str = Field(default="", description="繁體中文對照標題（沒有就留空）")
    provider: str = Field(default="", description="產生此案例的來源 provider 名稱")
    creator: str = Field(default="unknown", description="創作者帳號或名稱")
    market: str = Field(default="VN_IN_TW", description="目標市場（VN_IN_TW 或 VN_LOCAL）")
    source_type: SourceType = Field(default=SourceType.ORGANIC, description="自然流量 (organic) 或廣告 (ad)")
    published_at: str | None = Field(default=None, description="原始發布時間 ISO 字串（未知則為 None）")
    collected_at: str = Field(description="擷取時間 ISO 字串")
    views: int | None = Field(default=None, ge=0, description="觀看數（缺失必須為 None，不得填 0）")
    likes: int | None = Field(default=None, ge=0, description="按讚數（缺失必須為 None，不得填 0）")
    comments: int | None = Field(default=None, ge=0, description="留言數（缺失必須為 None，不得填 0）")
    shares: int | None = Field(default=None, ge=0, description="分享數（缺失必須為 None，不得填 0）")
    follower_count: int | None = Field(default=None, ge=0, description="創作者粉絲數（缺失必須為 None）")
    attributed_new_followers: int | None = Field(
        default=None, ge=0, description="可合理歸因新增追蹤數（無歸因資料必須為 None，不得宣稱確認）"
    )
    missing_fields: list[str] = Field(default_factory=list, description="缺失或未驗證之欄位清單")
    hook_notes: str = Field(
        default="", description="3 秒鉤子／第一張封面與結構證據筆記（未驗證時須明確註明）"
    )
    narrative_category: str = Field(default="general", description="敘事類別（如清單實用、情緒共鳴、前後對比）")
    evidence_level: EvidenceLevel = Field(default=EvidenceLevel.UNVERIFIED, description="證據可信度等級")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="資料可信度分數 0.0–1.0")
    is_mock: bool = Field(default=False, description="是否為離線測試/Mock 樣本")


class EngagementMetricsResult(BaseModel):
    """研究案例互動率與追蹤轉換計算結果（MASTER_SPEC.md 第 5.4 節）。"""

    rate_type: EngagementRateType
    rate_value: float | None = Field(default=None, description="互動率數值（無法計算時為 None）")
    present_numerator_fields: list[str] = Field(default_factory=list)
    missing_numerator_fields: list[str] = Field(default_factory=list)
    follower_conversion_rate: float | None = Field(
        default=None, description="新增追蹤轉換率（無歸因資料時必為 None）"
    )
    note: str = Field(description="口徑說明（防止跨口徑誤比）")


class TopicScoreBreakdown(BaseModel):
    """選題研究性評分拆解（MASTER_SPEC.md 第 5.4 節）。

    權重配置：
    - 在台受眾相關性 (taiwan_audience_relevance): 35%
    - 追蹤動機 (follow_motivation): 25%
    - 分享收藏誘因 (share_save_motivation): 20%
    - 六張輪播適配度 (carousel_adaptability): 10%
    - 資料可信度 (data_credibility): 10%
    註：此為可調內部研究假設分數（0–100），不是 TikTok 官方演算法分數。
    """

    taiwan_audience_relevance: float = Field(ge=0.0, le=100.0)
    follow_motivation: float = Field(ge=0.0, le=100.0)
    share_save_motivation: float = Field(ge=0.0, le=100.0)
    carousel_adaptability: float = Field(ge=0.0, le=100.0)
    data_credibility: float = Field(ge=0.0, le=100.0)
    weighted_total: float = Field(ge=0.0, le=100.0)
    disclaimer: str = Field(
        default="內部研究性假設評分（35/25/20/10/10 權重），非 TikTok 官方演算法分數。"
    )


class TopicOpportunity(BaseModel):
    """主題機會模型（對應 MASTER_SPEC.md 第 7 節 `topic_opportunities`）。"""

    topic_id: str = Field(description="主題機會唯一識別碼")
    title_vi: str = Field(min_length=1, description="候選越南文主題名稱")
    title_zh_tw: str = Field(min_length=1, description="候選繁體中文主題翻譯")
    target_audience: str = Field(
        default="在台越南移工、留學生、新住民與青年", description="目標受眾"
    )
    angle: str = Field(min_length=1, description="切角與六頁輪播轉化方向")
    recommended_slot: ContentSlot = Field(default=ContentSlot.NOON_1130)
    research_case_ids: list[str] = Field(default_factory=list, description="關聯之研究案例 ID 清單")
    score_breakdown: TopicScoreBreakdown
    dedup_90d_passed: bool = Field(description="是否通過 90 天內主題與 URL 查重")
    dedup_reason: str = Field(default="90 天內無重複主題或來源 URL", description="查重判定說明")
    experiment_hypothesis: str = Field(description="實驗假設（例如：實用清單型首頁能否提升收藏率）")
    status: TopicCandidateStatus = Field(default=TopicCandidateStatus.CANDIDATE)
    created_at: str = Field(description="建立時間 ISO 字串")


class SlideItem(BaseModel):
    """單頁輪播圖文結構（頁碼 1..N，N 依內容決定）。"""

    index: int = Field(ge=1, le=TIKTOK_PHOTO_HARD_LIMIT, description="頁碼 1 到 N")
    vi_text: str = Field(min_length=1, description="越南文圖中文字")
    zh_tw_translation: str = Field(min_length=1, description="繁體中文逐頁翻譯對照")
    image_path: Path | None = Field(default=None, description="本地圖片路徑")
    sha256: str | None = Field(default=None, description="圖片檔案 SHA-256 雜湊值")


class MediaValidationItem(BaseModel):
    """單張圖片驗證結果明細。"""

    filename: str
    path: str
    index: int
    width: int
    height: int
    mime_type: str
    file_size_bytes: int
    sha256: str


class MediaValidationReport(BaseModel):
    """六張照片輪播品質 Gate 驗證報告。"""

    directory: str
    passed: bool
    expected_count: int = 6
    actual_count: int = 0
    items: list[MediaValidationItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CarouselPostPackage(BaseModel):
    """照片輪播完整貼文資料包（對應 content_posts 核心結構；張數依內容切分決定）。"""

    post_id: str = Field(description="貼文唯一識別碼")
    account_id: str = Field(default=DEFAULT_ACCOUNT_ID, description="所屬 TikTok 帳號 ID")
    slot: ContentSlot = Field(default=ContentSlot.NOON_1130)
    scheduled_at_taipei: str = Field(description="預定發布時間（Asia/Taipei ISO 字串）")
    vi_title: str = Field(max_length=90, description="越南文標題（TikTok 照片標題上限 90 字元）")
    zh_tw_title: str = Field(description="繁體中文標題翻譯")
    vi_caption: str = Field(max_length=4000, description="越南文貼文內容（不含或含標籤）")
    zh_tw_caption: str = Field(description="繁體中文貼文翻譯")
    hashtags: list[str] = Field(description="恰好五個簡明相關 hashtag")
    slides: list[SlideItem] = Field(description="逐頁圖文對照清單（張數 3–10）")
    media_dir: Path = Field(description="發布語系獨立 JPEG 所在目錄")
    core_angle: str = Field(default="", description="核心切角")
    cta: str = Field(default="", description="行動呼籲")
    is_mock: bool = Field(default=True, description="是否為測試/Mock 內容（嚴禁將 Mock 當作正式成品）")
    status: PostStatus = Field(default=PostStatus.DRAFT)

    @property
    def slide_count(self) -> int:
        return len(self.slides)

    @field_validator("hashtags")
    @classmethod
    def validate_five_hashtags(cls, v: list[str]) -> list[str]:
        """強制檢查恰好五個簡明相關 hashtag（MASTER_SPEC.md 第 3 節）。"""
        if len(v) != 5:
            raise ValueError(f"必須恰好提供 5 個 hashtag，目前數量為 {len(v)}")
        normalized: list[str] = []
        for tag in v:
            clean = tag.strip()
            if not clean:
                raise ValueError("Hashtag 不得為空白")
            if not clean.startswith("#"):
                clean = f"#{clean}"
            normalized.append(clean)
        return normalized

    @field_validator("slides")
    @classmethod
    def validate_slide_sequence(cls, v: list[SlideItem]) -> list[SlideItem]:
        """檢查張數介於 MIN_SLIDE_COUNT–MAX_SLIDE_COUNT，且頁碼連續為 1..N。"""
        if not MIN_SLIDE_COUNT <= len(v) <= MAX_SLIDE_COUNT:
            raise ValueError(
                f"輪播張數必須介於 {MIN_SLIDE_COUNT}–{MAX_SLIDE_COUNT} 張，目前為 {len(v)} 張"
            )
        indices = [item.index for item in v]
        expected = list(range(1, len(v) + 1))
        if indices != expected:
            raise ValueError(f"輪播頁碼必須連續為 {expected}，目前為 {indices}")
        return v


class PublishExecutionPlan(BaseModel):
    """發布執行計畫與結果報告（支援 dry-run 與真實狀態回報）。"""

    post_id: str
    adapter_name: str
    dry_run: bool
    allow_live_publish: bool
    status: PostStatus
    privacy_level: Literal["PUBLIC_TO_EVERYONE", "SELF_ONLY", "MANUAL_APP", "MOCK_ONLY"]
    media_validation: MediaValidationReport
    endpoint_or_target: str
    payload_preview: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    executed_at: datetime = Field(default_factory=datetime.now)


class AnalyticsSourceTier(StrEnum):
    """成效指標資料來源層級（對應 MASTER_SPEC.md 第 4, 11 節：嚴格區分後台可驗證、公開與估計）。"""

    CREATOR_BACKEND_VERIFIED = "creator_backend_verified"
    PUBLIC_OBSERVED = "public_observed"
    ESTIMATED_OR_UNVERIFIED = "estimated_or_unverified"
    MOCK_SIMULATION = "mock_simulation"


class AttributionConfidence(StrEnum):
    """自身新增粉絲歸因可信度等級（對應 MASTER_SPEC.md 第 5.4, 11 節）。"""

    DIRECT_POST_ATTRIBUTION = "direct_post_attribution"
    TIME_WINDOW_CORRELATION = "time_window_correlation"
    UNAVAILABLE = "unavailable"


class PostPerformanceObservation(BaseModel):
    """貼文發布後觀察期成效紀錄（對應 MASTER_SPEC.md 第 7, 11 節 `post_observations`）。

    嚴格規範：
    - 缺失指標保留為 `None`，嚴禁預設填 `0`。
    - 明確標註 `source_tier`（創作者後台可驗證 / 公開數據 / 估計或模擬）與 `attribution_confidence`。
    """

    observation_id: str = Field(description="觀察紀錄唯一識別碼")
    post_id: str = Field(description="關聯之貼文 ID (`content_posts.post_id`)")
    slot: ContentSlot = Field(default=ContentSlot.NOON_1130, description="貼文發布時段")
    observed_at: str = Field(description="指標擷取時間 ISO 字串")
    window_hours: int = Field(default=24, ge=1, description="發布後觀察視窗小時數（如 24h / 72h / 168h）")
    source_tier: AnalyticsSourceTier = Field(
        default=AnalyticsSourceTier.PUBLIC_OBSERVED,
        description="資料來源層級（區分後台驗證、公開數據、估計與模擬）",
    )
    views: int | None = Field(default=None, ge=0, description="觀看數（缺失保留 None，禁止預設填 0）")
    likes: int | None = Field(default=None, ge=0, description="按讚數（缺失保留 None）")
    comments: int | None = Field(default=None, ge=0, description="留言數（缺失保留 None）")
    shares: int | None = Field(default=None, ge=0, description="分享數（缺失保留 None）")
    saves: int | None = Field(default=None, ge=0, description="收藏數（缺失保留 None）")
    attributed_new_followers: int | None = Field(
        default=None, ge=0, description="新增粉絲數（無歸因資料時必為 None）"
    )
    attribution_confidence: AttributionConfidence = Field(
        default=AttributionConfidence.UNAVAILABLE,
        description="新增粉絲歸因可信度",
    )
    top_audience_regions: dict[str, float] = Field(
        default_factory=dict,
        description="受眾地區分佈比例（例如 {'TW': 0.68, 'VN': 0.27}，僅後台數據可得）",
    )
    experiment_variant: str = Field(
        default="A_LIST_HOOK",
        description="A/B 實驗變體代號（如 A_LIST_HOOK 數字清單鉤子 vs. B_STORY_HOOK 情境故事鉤子）",
    )
    is_mock: bool = Field(default=False, description="是否為離線模擬數據")


class SlotPerformanceSummary(BaseModel):
    """單一時段／主題分類之表現彙整。"""

    slot: ContentSlot
    post_count: int
    full_rate_post_count: int
    partial_rate_post_count: int
    avg_full_engagement_rate: float | None = Field(
        default=None, description="完整互動率平均值（僅針對具備完整讚/留言/分享/觀看之貼文計算）"
    )
    avg_save_share_ratio: float | None = Field(
        default=None, description="平均（收藏+分享）/觀看比率"
    )
    verified_new_followers_total: int = Field(
        default=0, description="具 DIRECT_POST_ATTRIBUTION 之累積新增粉絲數"
    )
    correlated_new_followers_total: int = Field(
        default=0, description="僅具時窗相關性（TIME_WINDOW_CORRELATION）之新增粉絲數（不混入直接歸因）"
    )


class WeightAdjustmentProposal(BaseModel):
    """下週選題研究權重與 A/B 實驗調整建議（MASTER_SPEC.md 第 4, 5.4, 11 節）。"""

    report_id: str
    generated_at: str
    current_weights: dict[str, float]
    proposed_weights: dict[str, float]
    rationale: list[str]
    slot_summaries: list[SlotPerformanceSummary]
    ab_test_summary: dict[str, object]
    next_week_experiments: list[str]
    is_based_on_mock: bool = Field(
        default=True,
        description="是否基於模擬數據（若為 True 則僅供演練，不宣稱為真實帳號營運結論）",
    )
