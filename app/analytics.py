"""Phase 5｜Analytics Agent：成效回饋、增粉歸因可信度分級、主題表現對照與 A/B 實驗權重優化引擎。

遵循 MASTER_SPEC.md 第 4, 5.4, 7, 11 節規範：
1. **嚴格區分資料來源層級 (`AnalyticsSourceTier`)**：
   - `CREATOR_BACKEND_VERIFIED`：創作者後台或官方授權 API 可驗證數據（含儲存數、受眾地區、單篇新增追蹤）。
   - `PUBLIC_OBSERVED`：公開頁面可見數據（觀看、讚、留言、分享；公開頁面若無新增粉絲歸因，則 `attributed_new_followers=None`）。
   - `ESTIMATED_OR_UNVERIFIED` / `MOCK_SIMULATION`：估算或離線演練樣本，明確標記 `is_mock=True`，絕不偽裝為真實營運結論。
2. **嚴格區分互動率口徑與新增粉絲歸因 (`AttributionConfidence`)**：
   - 缺失指標保留 `None`，禁止預設填 `0`。
   - 完整互動率（Full Engagement Rate）與部分互動率（Partial Engagement Rate）分開統計，禁止跨口徑直接比較平均值。
   - 新增粉絲區分為 `DIRECT_POST_ATTRIBUTION`（單篇後台直接歸因）、`TIME_WINDOW_CORRELATION`（僅時段相關性）與 `UNAVAILABLE`（無歸因資料），不將高觀看直接等同於帶粉。
3. **主題時段表現與 A/B 實驗回饋 (`AnalyticsAgent`)**：
   - 對照三時段（07:00 家庭情緒 / 11:30 台灣生活 / 18:00 圖文行銷）之互動率、收藏分享比與帶粉效率。
   - 比較 A/B 封面鉤子變體（如 `A_LIST_HOOK` 數字清單鉤子 vs. `B_STORY_HOOK` 共鳴故事鉤子）。
   - 根據實測表現提出下一週選題評分權重微調建議（維持總和 1.00，且單次調整幅度受控以保持可解釋性）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.errors import AutotokSystemError
from app.models import (
    AnalyticsSourceTier,
    AttributionConfidence,
    ContentSlot,
    EngagementMetricsResult,
    EngagementRateType,
    PostPerformanceObservation,
    SlotPerformanceSummary,
    WeightAdjustmentProposal,
)

DEFAULT_SCORING_WEIGHTS: dict[str, float] = {
    "taiwan_audience_relevance": 0.35,
    "follow_motivation": 0.25,
    "share_save_motivation": 0.20,
    "carousel_adaptability": 0.10,
    "data_credibility": 0.10,
}


def compute_post_observation_metrics(obs: PostPerformanceObservation) -> EngagementMetricsResult:
    """計算單筆發布後觀察紀錄的互動率與新增追蹤轉換率（嚴格遵守不預設填 0、不跨口徑混算原則）。"""
    if obs.views is None or obs.views <= 0:
        return EngagementMetricsResult(
            rate_type=EngagementRateType.UNAVAILABLE,
            rate_value=None,
            present_numerator_fields=[],
            missing_numerator_fields=["views", "likes", "comments", "shares"],
            follower_conversion_rate=None,
            note="觀看數缺失或為 0，無法計算互動率與追蹤轉換率。",
        )

    num_map = {
        "likes": obs.likes,
        "comments": obs.comments,
        "shares": obs.shares,
    }
    present = [k for k, v in num_map.items() if v is not None]
    missing = [k for k, v in num_map.items() if v is None]

    # 計算新增追蹤轉換率：僅在具備 DIRECT_POST_ATTRIBUTION 時視為確認轉換率
    follower_conv: float | None = None
    if (
        obs.attributed_new_followers is not None
        and obs.attribution_confidence == AttributionConfidence.DIRECT_POST_ATTRIBUTION
    ):
        follower_conv = round(obs.attributed_new_followers / obs.views, 6)

    if not present:
        return EngagementMetricsResult(
            rate_type=EngagementRateType.UNAVAILABLE,
            rate_value=None,
            present_numerator_fields=[],
            missing_numerator_fields=missing,
            follower_conversion_rate=follower_conv,
            note="讚、留言、分享全部缺失，無法計算互動率。",
        )

    numerator_sum = sum(v for v in num_map.values() if v is not None)
    rate_val = round(numerator_sum / obs.views, 6)

    if not missing:
        return EngagementMetricsResult(
            rate_type=EngagementRateType.FULL,
            rate_value=rate_val,
            present_numerator_fields=present,
            missing_numerator_fields=[],
            follower_conversion_rate=follower_conv,
            note="完整互動率 = (likes + comments + shares) / views。",
        )

    return EngagementMetricsResult(
        rate_type=EngagementRateType.PARTIAL,
        rate_value=rate_val,
        present_numerator_fields=present,
        missing_numerator_fields=missing,
        follower_conversion_rate=follower_conv,
        note=f"部分互動率（缺失欄位：{', '.join(missing)}），嚴禁與完整互動率跨口徑直接比較。",
    )


def build_mock_observations_fixture() -> list[PostPerformanceObservation]:
    """產生 9 筆涵蓋三時段、不同資料層級（後台驗證 vs. 公開缺失欄位）與 A/B 實驗變體的模擬觀察數據。"""
    now_iso = datetime.now(UTC).isoformat()
    return [
        # 11:30 台灣生活（高收藏、高分享、高在台受眾佔比）
        PostPerformanceObservation(
            observation_id="obs-mock-1130-01",
            post_id="post-2026-10-21-1130",
            slot=ContentSlot.NOON_1130,
            observed_at=now_iso,
            window_hours=72,
            source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
            views=18500,
            likes=1420,
            comments=185,
            shares=460,
            saves=890,
            attributed_new_followers=215,
            attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
            top_audience_regions={"TW": 0.74, "VN": 0.22, "OTHER": 0.04},
            experiment_variant="A_LIST_HOOK",
            is_mock=True,
        ),
        PostPerformanceObservation(
            observation_id="obs-mock-1130-02",
            post_id="post-2026-10-22-1130",
            slot=ContentSlot.NOON_1130,
            observed_at=now_iso,
            window_hours=72,
            source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
            views=14200,
            likes=980,
            comments=110,
            shares=310,
            saves=620,
            attributed_new_followers=148,
            attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
            top_audience_regions={"TW": 0.71, "VN": 0.25, "OTHER": 0.04},
            experiment_variant="A_LIST_HOOK",
            is_mock=True,
        ),
        # 07:00 家庭情緒（高共鳴留言，包含一筆公開觀察缺失 shares 的案例以驗證口徑分離）
        PostPerformanceObservation(
            observation_id="obs-mock-0700-01",
            post_id="post-2026-10-21-0700",
            slot=ContentSlot.MORNING_0700,
            observed_at=now_iso,
            window_hours=72,
            source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
            views=12600,
            likes=1150,
            comments=240,
            shares=210,
            saves=340,
            attributed_new_followers=96,
            attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
            top_audience_regions={"TW": 0.68, "VN": 0.28, "OTHER": 0.04},
            experiment_variant="B_STORY_HOOK",
            is_mock=True,
        ),
        PostPerformanceObservation(
            observation_id="obs-mock-0700-02",
            post_id="post-2026-10-22-0700",
            slot=ContentSlot.MORNING_0700,
            observed_at=now_iso,
            window_hours=48,
            source_tier=AnalyticsSourceTier.PUBLIC_OBSERVED,
            views=9800,
            likes=760,
            comments=135,
            shares=None,  # 模擬公開頁面擷取時分享數缺失，不得預設填 0
            saves=None,
            attributed_new_followers=42,
            attribution_confidence=AttributionConfidence.TIME_WINDOW_CORRELATION,
            top_audience_regions={},
            experiment_variant="B_STORY_HOOK",
            is_mock=True,
        ),
        # 18:00 圖文行銷（高精準追蹤轉換與收藏）
        PostPerformanceObservation(
            observation_id="obs-mock-1800-01",
            post_id="post-2026-10-21-1800",
            slot=ContentSlot.EVENING_1800,
            observed_at=now_iso,
            window_hours=72,
            source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
            views=11400,
            likes=890,
            comments=145,
            shares=295,
            saves=710,
            attributed_new_followers=175,
            attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
            top_audience_regions={"TW": 0.65, "VN": 0.30, "OTHER": 0.05},
            experiment_variant="A_LIST_HOOK",
            is_mock=True,
        ),
        PostPerformanceObservation(
            observation_id="obs-mock-1800-02",
            post_id="post-2026-10-22-1800",
            slot=ContentSlot.EVENING_1800,
            observed_at=now_iso,
            window_hours=72,
            source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
            views=8900,
            likes=610,
            comments=88,
            shares=140,
            saves=320,
            attributed_new_followers=74,
            attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
            top_audience_regions={"TW": 0.62, "VN": 0.33, "OTHER": 0.05},
            experiment_variant="B_STORY_HOOK",
            is_mock=True,
        ),
    ]


def load_observations_from_json(json_path: Path) -> list[PostPerformanceObservation]:
    """從外部真實或匯出之 JSON 檔案載入貼文觀察期指標。"""
    if not json_path.is_file():
        raise AutotokSystemError(
            f"找不到成效觀察 JSON 檔案：{json_path}",
            code="ANALYTICS_FILE_NOT_FOUND",
        )
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        items = raw["observations"] if isinstance(raw, dict) and "observations" in raw else raw
        if not isinstance(items, list):
            raise ValueError("JSON 頂層必須為 list 或包含 'observations' 陣列")
        return [PostPerformanceObservation.model_validate(item) for item in items]
    except Exception as exc:
        raise AutotokSystemError(
            f"解析成效觀察 JSON 失敗：{exc}",
            code="ANALYTICS_PARSE_ERROR",
        ) from exc


class AnalyticsAgent:
    """第五階段成效回饋與選題權重優化 Agent。"""

    def __init__(self, current_weights: dict[str, float] | None = None) -> None:
        self.current_weights = dict(current_weights or DEFAULT_SCORING_WEIGHTS)

    def summarize_by_slot(
        self, observations: list[PostPerformanceObservation]
    ) -> list[SlotPerformanceSummary]:
        """依每日三時段彙整表現，嚴格將完整互動率與部分互動率分開，且區分直接歸因與時窗相關的新增粉絲。"""
        summaries: list[SlotPerformanceSummary] = []
        for slot in [
            ContentSlot.MORNING_0700,
            ContentSlot.NOON_1130,
            ContentSlot.EVENING_1800,
        ]:
            slot_obs = [o for o in observations if o.slot == slot]
            full_rates: list[float] = []
            partial_count = 0
            save_share_ratios: list[float] = []
            verified_followers = 0
            correlated_followers = 0

            for o in slot_obs:
                m = compute_post_observation_metrics(o)
                if m.rate_type == EngagementRateType.FULL and m.rate_value is not None:
                    full_rates.append(m.rate_value)
                elif m.rate_type == EngagementRateType.PARTIAL:
                    partial_count += 1

                if o.views and o.views > 0 and o.saves is not None and o.shares is not None:
                    save_share_ratios.append((o.saves + o.shares) / o.views)

                if o.attributed_new_followers is not None:
                    if o.attribution_confidence == AttributionConfidence.DIRECT_POST_ATTRIBUTION:
                        verified_followers += o.attributed_new_followers
                    elif o.attribution_confidence == AttributionConfidence.TIME_WINDOW_CORRELATION:
                        correlated_followers += o.attributed_new_followers

            avg_full = round(sum(full_rates) / len(full_rates), 6) if full_rates else None
            avg_ss = (
                round(sum(save_share_ratios) / len(save_share_ratios), 6)
                if save_share_ratios
                else None
            )

            summaries.append(
                SlotPerformanceSummary(
                    slot=slot,
                    post_count=len(slot_obs),
                    full_rate_post_count=len(full_rates),
                    partial_rate_post_count=partial_count,
                    avg_full_engagement_rate=avg_full,
                    avg_save_share_ratio=avg_ss,
                    verified_new_followers_total=verified_followers,
                    correlated_new_followers_total=correlated_followers,
                )
            )
        return summaries

    def evaluate_ab_variants(
        self, observations: list[PostPerformanceObservation]
    ) -> dict[str, object]:
        """比較 A/B 實驗變體（例如 `A_LIST_HOOK` 數字清單鉤子 vs. `B_STORY_HOOK` 共鳴故事鉤子）之表現。"""
        variants: dict[str, list[PostPerformanceObservation]] = {}
        for o in observations:
            variants.setdefault(o.experiment_variant, []).append(o)

        variant_stats: dict[str, dict[str, object]] = {}
        for var_name, items in sorted(variants.items()):
            full_rates: list[float] = []
            direct_conv_rates: list[float] = []
            save_rates: list[float] = []
            for o in items:
                m = compute_post_observation_metrics(o)
                if m.rate_type == EngagementRateType.FULL and m.rate_value is not None:
                    full_rates.append(m.rate_value)
                if m.follower_conversion_rate is not None:
                    direct_conv_rates.append(m.follower_conversion_rate)
                if o.views and o.views > 0 and o.saves is not None:
                    save_rates.append(o.saves / o.views)

            variant_stats[var_name] = {
                "sample_count": len(items),
                "avg_full_engagement_rate": (
                    round(sum(full_rates) / len(full_rates), 6) if full_rates else None
                ),
                "avg_direct_follower_conversion_rate": (
                    round(sum(direct_conv_rates) / len(direct_conv_rates), 6)
                    if direct_conv_rates
                    else None
                ),
                "avg_save_rate": (
                    round(sum(save_rates) / len(save_rates), 6) if save_rates else None
                ),
            }

        winner = None
        best_conv = -1.0
        for var_name, st in variant_stats.items():
            conv = st.get("avg_direct_follower_conversion_rate")
            if isinstance(conv, float) and conv > best_conv:
                best_conv = conv
                winner = var_name

        return {
            "variants": variant_stats,
            "recommended_lead_variant": winner,
            "methodology_note": (
                "僅採計具備完整互動率口徑與 DIRECT_POST_ATTRIBUTION 後台歸因之樣本進行主決策，"
                "不將無歸因之單純高觀看等同於帶粉。"
            ),
        }

    def generate_weekly_optimization_proposal(
        self, observations: list[PostPerformanceObservation]
    ) -> WeightAdjustmentProposal:
        """根據本週觀察期指標生成下一週選題評分權重微調建議與實驗方向。"""
        slot_summaries = self.summarize_by_slot(observations)
        ab_summary = self.evaluate_ab_variants(observations)
        any_mock = any(o.is_mock for o in observations) or len(observations) == 0

        proposed = dict(self.current_weights)
        rationale: list[str] = []

        # 檢視整體收藏分享比與直接帶粉表現，進行小步受控權重微調（單週最大 ±0.03，並維持總和 1.00）
        all_ss = [
            s.avg_save_share_ratio
            for s in slot_summaries
            if s.avg_save_share_ratio is not None
        ]
        avg_ss_overall = sum(all_ss) / len(all_ss) if all_ss else 0.0

        total_verified_followers = sum(s.verified_new_followers_total for s in slot_summaries)
        total_correlated_followers = sum(s.correlated_new_followers_total for s in slot_summaries)

        if avg_ss_overall >= 0.05 and total_verified_followers > 0:
            # 當「收藏+分享」比率表現強勁且實際帶來經驗證的新增追蹤時，微幅提升 share_save_motivation 與 follow_motivation
            proposed["share_save_motivation"] = round(
                self.current_weights["share_save_motivation"] + 0.02, 2
            )
            proposed["follow_motivation"] = round(
                self.current_weights["follow_motivation"] + 0.01, 2
            )
            proposed["taiwan_audience_relevance"] = round(
                self.current_weights["taiwan_audience_relevance"] - 0.03, 2
            )
            rationale.append(
                f"本週具備後台直接歸因之新增粉絲共 {total_verified_followers} 人，且平均（收藏+分享）/觀看達 "
                f"{avg_ss_overall:.2%}，顯示高實用收藏型六頁圖卡能有效帶動轉粉，建議將「分享收藏誘因」權重由 "
                f"{self.current_weights['share_save_motivation']:.0%} 上調至 {proposed['share_save_motivation']:.0%}，"
                f"「追蹤動機」由 {self.current_weights['follow_motivation']:.0%} 上調至 {proposed['follow_motivation']:.0%}。"
            )
        else:
            rationale.append(
                "目前經後台直接歸因之樣本數或收藏分享比尚未達顯著調整門檻，維持既有 35/25/20/10/10 基準權重。"
            )

        if total_correlated_followers > 0:
            rationale.append(
                f"另有 {total_correlated_followers} 名新增粉絲僅具時段相關性（TIME_WINDOW_CORRELATION，無單篇後台直接歸因），"
                "依 MASTER_SPEC.md 第 5.4 節規範已單獨列計，不混入已確認之單篇帶粉轉換率。"
            )

        # 確保權重總和精確為 1.0
        total_w = round(sum(proposed.values()), 4)
        if total_w != 1.0:
            diff = round(1.0 - total_w, 4)
            proposed["taiwan_audience_relevance"] = round(
                proposed["taiwan_audience_relevance"] + diff, 4
            )

        next_experiments = [
            "07:00（家庭情緒）：維持故事共鳴開場（B_STORY_HOOK），在第 5 頁加入具體在台生活時間管理小工具以提高收藏率。",
            "11:30（台灣生活）：持續深耕台灣超商／交通／醫療實用步驟清單（A_LIST_HOOK），強化第 1 頁封面 1 秒視覺錨點。",
            "18:00（圖文行銷）：測試「前後對比（Messy vs. Clean SOP）」作為第 2–3 頁結構，觀察對留言與個人主頁點擊的提升幅度。",
        ]

        return WeightAdjustmentProposal(
            report_id=f"opt-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            generated_at=datetime.now(UTC).isoformat(),
            current_weights=self.current_weights,
            proposed_weights=proposed,
            rationale=rationale,
            slot_summaries=slot_summaries,
            ab_test_summary=ab_summary,
            next_week_experiments=next_experiments,
            is_based_on_mock=any_mock,
        )

    def render_markdown_report(self, proposal: WeightAdjustmentProposal) -> str:
        """將每週成效回饋與權重優化建議輸出為繁體中文 Markdown 報告。"""
        mode_banner = (
            "> [!WARNING]\n> **資料性質聲明**：本報告包含離線模擬樣本（`is_based_on_mock=True`），僅供驗證 Phase 5 歸因分級、口徑分離與權重調整演算法，**不得宣稱為真實 TikTok 帳號已發生之營運數據**。"
            if proposal.is_based_on_mock
            else "> [!IMPORTANT]\n> **資料性質聲明**：本報告全部基於真實擷取之觀察期數據（`is_based_on_mock=False`）。"
        )

        slot_rows: list[str] = []
        for s in proposal.slot_summaries:
            full_str = (
                f"{s.avg_full_engagement_rate:.2%}"
                if s.avg_full_engagement_rate is not None
                else "N/A（無完整口徑樣本）"
            )
            ss_str = (
                f"{s.avg_save_share_ratio:.2%}"
                if s.avg_save_share_ratio is not None
                else "N/A"
            )
            slot_rows.append(
                f"| `{s.slot.value}` | {s.post_count} | {s.full_rate_post_count} 篇完整 / {s.partial_rate_post_count} 篇部分 | "
                f"{full_str} | {ss_str} | **{s.verified_new_followers_total}** 人 | {s.correlated_new_followers_total} 人 |"
            )

        weights_rows: list[str] = []
        for k in self.current_weights:
            cur_v = proposal.current_weights[k]
            prop_v = proposal.proposed_weights[k]
            delta = prop_v - cur_v
            delta_str = f"{delta:+.0%}" if abs(delta) >= 0.005 else "維持不變"
            weights_rows.append(
                f"| `{k}` | {cur_v:.0%} | **{prop_v:.0%}** | {delta_str} |"
            )

        ab_variants = proposal.ab_test_summary.get("variants", {})
        ab_rows: list[str] = []
        if isinstance(ab_variants, dict):
            for v_name, st in ab_variants.items():
                if isinstance(st, dict):
                    eng = st.get("avg_full_engagement_rate")
                    conv = st.get("avg_direct_follower_conversion_rate")
                    sav = st.get("avg_save_rate")
                    ab_rows.append(
                        f"| `{v_name}` | {st.get('sample_count', 0)} | "
                        f"{f'{eng:.2%}' if isinstance(eng, float) else 'N/A'} | "
                        f"{f'{sav:.2%}' if isinstance(sav, float) else 'N/A'} | "
                        f"**{f'{conv:.2%}' if isinstance(conv, float) else 'N/A'}** |"
                    )

        rationale_md = "\n".join(f"- {r}" for r in proposal.rationale)
        exp_md = "\n".join(f"- {e}" for e in proposal.next_week_experiments)

        return f"""# Phase 5｜成效回饋、增粉歸因與下週選題權重優化報告

- **報告編號**：`{proposal.report_id}`
- **產出時間**：`{proposal.generated_at}`

{mode_banner}

---

## 1. 三時段主題表現對照（嚴格分離完整互動率與部分互動率口徑）

| 時段與分類 | 總觀察篇數 | 口徑分佈（禁止跨口徑混算） | 平均完整互動率 | 平均（收藏+分享）/觀看 | 直接歸因新增粉絲 (`DIRECT`) | 時窗相關新增粉絲 (`CORRELATED`) |
|---|---:|---|---:|---:|---:|---:|
{chr(10).join(slot_rows)}

---

## 2. 封面鉤子 A/B 實驗表現與帶粉轉換對照

- **推薦領先變體**：`{proposal.ab_test_summary.get('recommended_lead_variant')}`
- **評估準則**：{proposal.ab_test_summary.get('methodology_note')}

| 實驗變體代號 | 樣本數 | 平均完整互動率 | 平均收藏率 (`saves/views`) | 直接歸因追蹤轉換率 (`new_followers/views`) |
|---|---:|---:|---:|---:|
{chr(10).join(ab_rows)}

---

## 3. 下週選題研究性評分權重調整建議

> 註：此為內部選題研究性假設權重（總和 100%），非 TikTok 官方演算法分數。

| 評分維度 | 目前權重 | 建議下週權重 | 調整幅度 |
|---|---:|---:|---|
{chr(10).join(weights_rows)}

### 調整依據與歸因分析
{rationale_md}

---

## 4. 下週三時段內容與 A/B 實驗策略
{exp_md}
"""
