"""Phase 2 市場研究 Agent、成長策略評分、90 天去重與 Notion 真實 Schema 探測測試套件。

涵蓋 MASTER_SPEC.md 第 4, 5, 7, 11 節要求：
1. 12 組原始越南市場搜尋關鍵字種子初始化與擴充。
2. 互動率口徑嚴格區分（完整互動率 vs. 部分互動率 vs. 無效觀看；缺失指標絕不預設為 0；無歸因資料時新增追蹤轉換率必為 None）。
3. 選題研究性評分（35% 在台受眾相關性 / 25% 追蹤動機 / 20% 分享收藏 / 10% 輪播適配 / 10% 資料可信度）。
4. 90 天內主題名稱與來源 URL 去重檢查。
5. 30–50 篇批次研究與不足篇數時如實標示（`JsonFileResearchProvider` 載入少於 30 篇時明確回報 `shortfall_notice`）。
6. Top Ads 廣告樣本（`SourceType.AD`）與自然流量（`SourceType.ORGANIC`）嚴格分離。
7. Notion 資料庫真實 Schema 探測器：無 Token 時拋出 `BlockedNeedsAuthError`；有回應時唯讀解析真實欄位且不猜測未存在欄位。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.cli import main as cli_main
from app.config import AppSettings
from app.errors import BlockedNeedsAuthError
from app.models import (
    EngagementRateType,
    EvidenceLevel,
    ResearchCase,
    SourceType,
    TopicCandidateStatus,
)
from app.notion_schema import NotionInspector
from app.research import (
    INITIAL_SEED_KEYWORDS,
    JsonFileResearchProvider,
    MockResearchProvider,
    build_topic_opportunities_from_cases,
    check_90_day_dedup,
    compute_engagement_metrics,
    compute_topic_score,
)
from app.state import PostStateRepository


def test_seed_keywords_count_and_persistence(tmp_path: Path) -> None:
    """驗證 12 組原始越南文種子關鍵字完整寫入與擴充。"""
    assert len(INITIAL_SEED_KEYWORDS) == 12
    repo = PostStateRepository(tmp_path / "phase2_kw.db")
    inserted = repo.upsert_seed_keywords(INITIAL_SEED_KEYWORDS)
    assert inserted == 12

    stored = repo.list_seed_keywords()
    assert len(stored) == 12
    vi_list = {k.keyword_vi for k in stored}
    assert "người Việt ở Đài Loan" in vi_list
    assert "người Việt nhớ nhà" in vi_list


def test_engagement_metrics_full_vs_partial_vs_unavailable() -> None:
    """驗證互動率計算：完整指標、缺失部分指標（部分互動率）、無歸因追蹤轉換率不預設為 0。"""
    # 1. 完整指標
    full_case = ResearchCase(
        case_id="c-full",
        source_url="https://www.tiktok.com/@user/video/1",
        collected_at="2026-10-20T00:00:00Z",
        views=10000,
        likes=500,
        comments=100,
        shares=200,
        attributed_new_followers=50,
        evidence_level=EvidenceLevel.VERIFIED_PUBLIC,
    )
    res_full = compute_engagement_metrics(full_case)
    assert res_full.rate_type == EngagementRateType.FULL
    assert res_full.rate_value == pytest.approx(0.08)
    assert res_full.follower_conversion_rate == pytest.approx(0.005)

    # 2. 缺失 shares 與 attributed_new_followers -> 必須標記為 PARTIAL，且追蹤轉換率為 None
    partial_case = ResearchCase(
        case_id="c-partial",
        source_url="https://www.tiktok.com/@user/video/2",
        collected_at="2026-10-20T00:00:00Z",
        views=10000,
        likes=500,
        comments=100,
        shares=None,
        attributed_new_followers=None,
        missing_fields=["shares", "attributed_new_followers"],
        evidence_level=EvidenceLevel.PARTIAL_PUBLIC,
    )
    res_partial = compute_engagement_metrics(partial_case)
    assert res_partial.rate_type == EngagementRateType.PARTIAL
    assert res_partial.rate_value == pytest.approx(0.06)
    assert "shares" in res_partial.missing_numerator_fields
    assert res_partial.follower_conversion_rate is None
    assert "不可與完整互動率跨口徑直接比較" in res_partial.note

    # 3. 缺失 views -> UNAVAILABLE
    no_view_case = ResearchCase(
        case_id="c-noview",
        source_url="https://www.tiktok.com/@user/video/3",
        collected_at="2026-10-20T00:00:00Z",
        views=None,
        likes=100,
    )
    res_unavail = compute_engagement_metrics(no_view_case)
    assert res_unavail.rate_type == EngagementRateType.UNAVAILABLE
    assert res_unavail.rate_value is None


def test_topic_score_weights_35_25_20_10_10() -> None:
    """驗證選題研究性評分嚴格遵循 35% / 25% / 20% / 10% / 10% 權重。"""
    breakdown = compute_topic_score(
        taiwan_audience_relevance=100.0,
        follow_motivation=80.0,
        share_save_motivation=70.0,
        carousel_adaptability=90.0,
        data_credibility=60.0,
    )
    # 100*0.35 (35) + 80*0.25 (20) + 70*0.20 (14) + 90*0.10 (9) + 60*0.10 (6) = 84.0
    assert breakdown.weighted_total == pytest.approx(84.0)
    assert "非 TikTok 官方演算法分數" in breakdown.disclaimer


def test_90_day_dedup_blocks_duplicate_url_and_similar_title() -> None:
    """驗證 90 天查重會攔截重複來源 URL 與高度相似之越南文主題。"""
    existing_topics = [
        ("topic-001", "6 tiện ích tại cửa hàng tiện lợi và chợ đêm Đài Loan giúp người Việt tiết kiệm thời gian")
    ]
    existing_urls = {"https://www.tiktok.com/@creator/video/999"}

    # URL 重複
    passed_url, reason_url = check_90_day_dedup(
        candidate_title_vi="Chủ đề hoàn toàn mới tại Đài Loan",
        candidate_urls=["https://www.tiktok.com/@creator/video/999"],
        existing_topics_within_90d=existing_topics,
        existing_case_urls_within_90d=existing_urls,
    )
    assert passed_url is False
    assert "90 天內已被其他選題使用" in reason_url

    # 主題高度重疊
    passed_title, reason_title = check_90_day_dedup(
        candidate_title_vi="6 tiện ích tại cửa hàng tiện lợi và chợ đêm Đài Loan giúp người Việt",
        candidate_urls=["https://www.tiktok.com/@creator/video/888"],
        existing_topics_within_90d=existing_topics,
        existing_case_urls_within_90d=existing_urls,
    )
    assert passed_title is False
    assert "topic-001" in reason_title


def test_batch_research_and_honest_shortfall_reporting(tmp_path: Path) -> None:
    """驗證 30-50 篇批次研究與真實案例不足 30 篇時的誠實回報機制。"""
    # 1. Mock 批次 35 篇（涵蓋自然流量與廣告分離）
    mock_provider = MockResearchProvider()
    cases = mock_provider.collect_cases(max_cases=35)
    assert len(cases) == 35
    assert any(c.source_type == SourceType.AD for c in cases)
    assert any(c.source_type == SourceType.ORGANIC for c in cases)

    opps = build_topic_opportunities_from_cases(cases)
    assert len(opps) == 3
    assert all(opp.status == TopicCandidateStatus.CANDIDATE for opp in opps)

    # 2. JSON 檔案僅有 2 篇真實案例時，如實回報 2 篇，不自動捏造湊滿 30 篇
    sample_json = tmp_path / "real_2_cases.json"
    sample_json.write_text(
        json.dumps(
            [
                {
                    "case_id": "real-001",
                    "source_url": "https://www.tiktok.com/@real_vn/video/101",
                    "creator": "@real_vn",
                    "market": "VN_IN_TW",
                    "source_type": "organic",
                    "collected_at": "2026-10-20T01:00:00Z",
                    "views": 22000,
                    "likes": 1200,
                    "comments": 80,
                    "shares": None,
                    "missing_fields": ["shares", "follower_count"],
                    "hook_notes": "真實公開案例 1",
                    "evidence_level": "partial_public",
                    "confidence": 0.9,
                    "is_mock": False,
                },
                {
                    "case_id": "real-002",
                    "source_url": "https://www.tiktok.com/@real_vn/video/102",
                    "creator": "@real_vn",
                    "market": "VN_IN_TW",
                    "source_type": "organic",
                    "collected_at": "2026-10-20T01:05:00Z",
                    "views": 18000,
                    "likes": 900,
                    "comments": 45,
                    "shares": 60,
                    "missing_fields": [],
                    "hook_notes": "真實公開案例 2",
                    "evidence_level": "verified_public",
                    "confidence": 0.95,
                    "is_mock": False,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    file_provider = JsonFileResearchProvider(sample_json)
    loaded = file_provider.collect_cases(max_cases=50)
    assert len(loaded) == 2
    assert loaded[0].shares is None


def test_notion_inspector_blocks_without_token_and_parses_real_schema() -> None:
    """驗證 NotionInspector 在無 NOTION_TOKEN 時拋出 BlockedNeedsAuthError，且能正確解析真實 Schema。"""
    settings_no_token = AppSettings(NOTION_TOKEN="")
    inspector = NotionInspector(settings=settings_no_token)
    with pytest.raises(BlockedNeedsAuthError):
        inspector.fetch_database_schema()

    # 使用 MockTransport 模擬 Notion `GET /v1/databases/{id}` 真實回應
    mock_notion_response = {
        "object": "database",
        "id": "f1bc3439-6f15-4e62-bca9-0c9deea19d7a",
        "title": [{"plain_text": "TikTok 越南市場內容庫"}],
        "properties": {
            "Name": {"id": "title", "type": "title", "title": {}},
            "Status": {"id": "st01", "type": "status", "status": {}},
            "預定發布時間": {"id": "dt01", "type": "date", "date": {}},
            "主題分類": {"id": "cat1", "type": "select", "select": {}},
            "六張輪播圖": {"id": "fl01", "type": "files", "files": {}},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "f1bc34396f154e62bca90c9deea19d7a" in str(request.url)
        return httpx.Response(200, json=mock_notion_response)

    settings_with_token = AppSettings(NOTION_TOKEN="ntn_test_token_secret")
    inspector_authed = NotionInspector(settings=settings_with_token)
    report = inspector_authed.fetch_database_schema(transport=httpx.MockTransport(handler))

    assert report.title == "TikTok 越南市場內容庫"
    assert len(report.properties) == 5
    assert "post_title_zh" in report.recommended_mapping
    assert "status" in report.recommended_mapping
    assert "scheduled_date" in report.recommended_mapping
    assert "vi_carousel_images" in report.recommended_mapping


def test_phase2_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """驗證 Phase 2 CLI 命令 (`seed-keywords`, `research-batch`, `inspect-notion`)。"""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli_p2.db"))
    monkeypatch.setenv("NOTION_TOKEN", "")

    assert cli_main(["seed-keywords", "--add-vi", "chợ đêm Đài Loan", "--add-zh", "台灣夜市"]) == 0
    assert cli_main(["research-batch", "--provider", "mock", "--max-cases", "35"]) == 0
    # 未提供 NOTION_TOKEN 時 inspect-notion 應回傳 exit code 2 (BLOCKED_NEEDS_AUTH)
    assert cli_main(["inspect-notion"]) == 2
