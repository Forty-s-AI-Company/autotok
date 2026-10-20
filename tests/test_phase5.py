"""Phase 5｜Analytics Agent 成效回饋、增粉歸因可信度分級與下週選題權重優化測試套件。

涵蓋 MASTER_SPEC.md 第 4, 5.4, 7, 11 節規範：
1. 完整互動率 vs. 部分互動率口徑分離（缺失指標保留 `None`，絕不預設填 `0`）。
2. 新增粉絲歸因可信度分級：
   - `DIRECT_POST_ATTRIBUTION`（創作者後台單篇直接歸因）可計算確認之追蹤轉換率。
   - `TIME_WINDOW_CORRELATION`（僅時段相關性）或 `UNAVAILABLE` 不得計入單篇確認追蹤轉換率，防止把高觀看直接等同帶粉。
3. 三時段表現彙整、A/B 實驗變體（`A_LIST_HOOK` vs. `B_STORY_HOOK`）對照與下週選題評分權重微調建議（權重總和嚴格維持 `1.0`）。
4. CLI `analytics-report` 命令與 SQLite `post_observations` 持久化驗證。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.analytics import (
    AnalyticsAgent,
    build_mock_observations_fixture,
    compute_post_observation_metrics,
    load_observations_from_json,
)
from app.cli import main as cli_main
from app.models import (
    AnalyticsSourceTier,
    AttributionConfidence,
    ContentSlot,
    EngagementRateType,
    PostPerformanceObservation,
)
from app.state import PostStateRepository


def test_observation_metrics_separates_full_partial_and_attribution() -> None:
    """驗證單筆觀察紀錄之互動率口徑分離與新增粉絲歸因可信度檢查。"""
    # 1. 具備完整指標與後台直接歸因
    obs_full = PostPerformanceObservation(
        observation_id="obs-full-1",
        post_id="post-1",
        slot=ContentSlot.NOON_1130,
        observed_at="2026-10-22T12:00:00Z",
        window_hours=48,
        source_tier=AnalyticsSourceTier.CREATOR_BACKEND_VERIFIED,
        views=10000,
        likes=800,
        comments=100,
        shares=300,
        saves=500,
        attributed_new_followers=120,
        attribution_confidence=AttributionConfidence.DIRECT_POST_ATTRIBUTION,
    )
    m_full = compute_post_observation_metrics(obs_full)
    assert m_full.rate_type == EngagementRateType.FULL
    assert m_full.rate_value == pytest.approx(0.12)
    assert m_full.follower_conversion_rate == pytest.approx(0.012)

    # 2. 公開觀察缺失 shares，且新增粉絲僅具時窗相關性（非單篇直接歸因）
    obs_partial = PostPerformanceObservation(
        observation_id="obs-part-1",
        post_id="post-2",
        slot=ContentSlot.MORNING_0700,
        observed_at="2026-10-22T12:00:00Z",
        window_hours=48,
        source_tier=AnalyticsSourceTier.PUBLIC_OBSERVED,
        views=10000,
        likes=500,
        comments=100,
        shares=None,
        saves=None,
        attributed_new_followers=50,
        attribution_confidence=AttributionConfidence.TIME_WINDOW_CORRELATION,
    )
    m_part = compute_post_observation_metrics(obs_partial)
    assert m_part.rate_type == EngagementRateType.PARTIAL
    assert m_part.rate_value == pytest.approx(0.06)
    assert "shares" in m_part.missing_numerator_fields
    # 僅具時窗相關性時，不得宣稱確認單篇追蹤轉換率
    assert m_part.follower_conversion_rate is None


def test_analytics_agent_summarizes_slots_and_adjusts_weights_to_sum_one() -> None:
    """驗證 AnalyticsAgent 能正確分離直接歸因與時窗相關粉絲，並提出總和為 1.0 的權重建議。"""
    obs_list = build_mock_observations_fixture()
    agent = AnalyticsAgent()
    proposal = agent.generate_weekly_optimization_proposal(obs_list)

    assert proposal.is_based_on_mock is True
    assert sum(proposal.proposed_weights.values()) == pytest.approx(1.0)

    # 檢查 07:00 時段：1 篇完整互動率（96 名直接歸因粉絲）、1 篇部分互動率（42 名時窗相關粉絲）
    morning_summary = next(
        s for s in proposal.slot_summaries if s.slot == ContentSlot.MORNING_0700
    )
    assert morning_summary.post_count == 2
    assert morning_summary.full_rate_post_count == 1
    assert morning_summary.partial_rate_post_count == 1
    assert morning_summary.verified_new_followers_total == 96
    assert morning_summary.correlated_new_followers_total == 42

    # 檢查 A/B 實驗變體評估
    assert proposal.ab_test_summary["recommended_lead_variant"] == "A_LIST_HOOK"

    md = agent.render_markdown_report(proposal)
    assert "Phase 5｜成效回饋、增粉歸因與下週選題權重優化報告" in md
    assert "A_LIST_HOOK" in md


def test_post_observations_sqlite_persistence_preserves_nulls(tmp_path: Path) -> None:
    """驗證 SQLite post_observations 資料表能正確儲存並還原 None 缺失欄位（不預設填 0）。"""
    repo = PostStateRepository(tmp_path / "p5_state.db")
    obs = PostPerformanceObservation(
        observation_id="obs-null-check",
        post_id="post-null",
        slot=ContentSlot.EVENING_1800,
        observed_at="2026-10-22T18:00:00Z",
        window_hours=24,
        source_tier=AnalyticsSourceTier.PUBLIC_OBSERVED,
        views=5000,
        likes=300,
        comments=None,
        shares=None,
        saves=None,
        attributed_new_followers=None,
        attribution_confidence=AttributionConfidence.UNAVAILABLE,
        is_mock=False,
    )
    repo.upsert_post_observation(obs)
    loaded = repo.list_post_observations(include_mock=False)
    assert len(loaded) == 1
    assert loaded[0].observation_id == "obs-null-check"
    assert loaded[0].comments is None
    assert loaded[0].shares is None
    assert loaded[0].attributed_new_followers is None


def test_analytics_report_cli_mock_and_json_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """驗證 CLI `analytics-report` 支援 mock 模式與外部 JSON 匯入模式。"""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli_p5.db"))
    out_md = tmp_path / "report_mock.md"

    rc = cli_main(["analytics-report", "--mode", "mock", "--output-md", str(out_md)])
    assert rc == 0
    assert out_md.is_file()
    assert "權重調整建議" in out_md.read_text(encoding="utf-8")

    # 測試 JSON 檔案匯入模式
    custom_json = tmp_path / "custom_obs.json"
    sample_data = [o.model_dump() for o in build_mock_observations_fixture()[:2]]
    custom_json.write_text(json.dumps(sample_data, ensure_ascii=False), encoding="utf-8")
    loaded_from_file = load_observations_from_json(custom_json)
    assert len(loaded_from_file) == 2

    out_md_json = tmp_path / "report_json.md"
    rc_json = cli_main(
        [
            "analytics-report",
            "--mode",
            "json",
            "--input-json",
            str(custom_json),
            "--output-md",
            str(out_md_json),
        ]
    )
    assert rc_json == 0
    assert out_md_json.is_file()
