"""研究 provider、帳號適配度與研究頁路由的測試。全程不呼叫真實網路或 LLM，也不讀取 .env。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.fit import FLAG_AD, FLAG_COMPLIANCE, detect_language, score_case
from app.markets import profile_from_template
from app.models import ContentSlot, EvidenceLevel, ResearchCase, SourceType
from app.research_providers import (
    ImportProvider,
    LlmWebSearchProvider,
    MockSourceProvider,
    ThirdPartyApiProvider,
    extract_json,
    normalize_case,
    parse_count,
)
from app.research_service import run_research, topic_from_case
from app.state import PostStateRepository
from app.web.main import create_app

VI = profile_from_template("vi-tw")


def settings(tmp_path: Path, **kw: object) -> AppSettings:
    return AppSettings(_env_file=None, SQLITE_DB_PATH=tmp_path / "t.db", **kw)


def case(**kw: object) -> ResearchCase:
    base = {
        "case_id": "c1",
        "source_url": "https://www.tiktok.com/@a/photo/1",
        "collected_at": "2026-10-20T00:00:00+00:00",
        "title": "5 tiện ích ở 7-Eleven Đài Loan mà người Việt hay bỏ lỡ",
        "narrative_category": "practical_checklist",
        "evidence_level": EvidenceLevel.VERIFIED_PUBLIC,
        "confidence": 0.85,
        "views": 482_000,
        "likes": 30_000,
        "comments": 4_000,
        "shares": 6_500,
    }
    base.update(kw)
    return ResearchCase(**base)


# --- 數字與正規化 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("482K", 482_000), ("1.2M", 1_200_000), ("1,234", 1234), ("3.5萬", 35_000), (12, 12), (None, None), ("", None), ("abc", None), (-5, None), (True, None)],
)
def test_parse_count_never_invents_zero(raw: object, expected: int | None) -> None:
    assert parse_count(raw) == expected


def test_normalize_drops_items_without_url_and_keeps_missing_as_none() -> None:
    assert normalize_case({"title": "x"}, provider="t", market=VI, default_level=EvidenceLevel.PARTIAL_PUBLIC) is None
    assert normalize_case({"url": "javascript:alert(1)"}, provider="t", market=VI, default_level=EvidenceLevel.PARTIAL_PUBLIC) is None
    c = normalize_case(
        {"url": "https://a.com/1", "views": "10K", "likes": None},
        provider="t", market=VI, default_level=EvidenceLevel.PARTIAL_PUBLIC,
    )  # fmt: skip
    assert c is not None
    assert c.views == 10_000 and c.likes is None and c.comments is None and c.shares is None
    assert {"likes", "comments", "shares"} <= set(c.missing_fields)
    assert c.evidence_level == EvidenceLevel.PARTIAL_PUBLIC


def test_metrics_not_from_page_are_discarded_and_ad_is_separated() -> None:
    c = normalize_case(
        {"url": "https://a.com/1", "views": 999, "likes": 9, "is_ad": True},
        provider="t", market=VI, default_level=EvidenceLevel.PARTIAL_PUBLIC, metrics_from_page=False,
    )  # fmt: skip
    assert c is not None
    assert c.views is None and c.likes is None
    assert c.source_type == SourceType.AD and c.evidence_level == EvidenceLevel.AD_LIBRARY


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json('說明\n```json\n{"cases": [{"url": "https://a.com"}]}\n```') == {"cases": [{"url": "https://a.com"}]}
    with pytest.raises(ValueError):
        extract_json("沒有 JSON")


# --- ImportProvider ---


def test_import_provider_url_lines() -> None:
    cases = ImportProvider(VI, "https://a.com/1\n\nhttps://a.com/2\nnot a url\nhttps://a.com/1/").collect_cases()
    assert [c.source_url for c in cases] == ["https://a.com/1", "https://a.com/2"]
    assert all(c.views is None and c.evidence_level == EvidenceLevel.MANUAL_IMPORT for c in cases)
    assert all("views" in c.missing_fields for c in cases)


def test_import_provider_csv_with_aliases_and_blank_cells() -> None:
    csv_text = "link,標題,播放,讚,留言,分享\nhttps://a.com/1,Tiêu đề,\"1,200\",100,,20\n,沒有網址,5,1,1,1\n"
    cases = ImportProvider(VI, csv_text).collect_cases()
    assert len(cases) == 1
    c = cases[0]
    assert c.title == "Tiêu đề" and c.views == 1200 and c.likes == 100 and c.shares == 20
    assert c.comments is None and "comments" in c.missing_fields


def test_import_provider_json() -> None:
    payload = json.dumps({"cases": [{"url": "https://a.com/1", "title": "T", "views": "2K", "is_ad": "true"}]})
    c = ImportProvider(VI, payload).collect_cases()[0]
    assert c.views == 2000 and c.source_type == SourceType.AD


# --- LlmWebSearchProvider ---


def test_llm_provider_unavailable_without_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.research_providers.find_codex_bin", lambda *_a, **_k: None)
    p = LlmWebSearchProvider(settings(tmp_path), VI)
    a = p.availability()
    assert not a.ok and "ANTHROPIC_API_KEY" in a.reason
    with pytest.raises(RuntimeError):
        p.collect_cases()


def test_llm_provider_anthropic_parses_and_drops_bad_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.research_providers.find_codex_bin", lambda *_a, **_k: None)
    answer = {
        "cases": [
            {"url": "https://a.com/1", "title": "T1", "views": 1000, "likes": 50, "comments": 5, "shares": 2, "metrics_source": "page", "narrative_category": "food_guide"},
            {"url": "https://a.com/2", "title": "估計值", "views": 777, "metrics_source": "none"},
            {"title": "沒有網址", "views": 5, "metrics_source": "page"},
        ]
    }
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "```json\n" + json.dumps(answer) + "\n```"}]})

    p = LlmWebSearchProvider(settings(tmp_path, ANTHROPIC_API_KEY="sk-test"), VI, transport=httpx.MockTransport(handler))
    assert p.backend() == "anthropic" and p.availability().ok
    cases = p.collect_cases(max_cases=10)
    assert seen["key"] == "sk-test"
    assert seen["body"]["tools"][0]["name"] == "web_search"  # type: ignore[index]
    assert [c.source_url for c in cases] == ["https://a.com/1", "https://a.com/2"]
    assert cases[0].evidence_level == EvidenceLevel.PARTIAL_PUBLIC and cases[0].views == 1000
    assert all(c.confidence <= 0.6 for c in cases)  # 程式沒有驗證來源頁面，不給 VERIFIED_PUBLIC
    assert cases[1].views is None and cases[1].evidence_level == EvidenceLevel.UNVERIFIED
    assert all(not c.is_mock for c in cases)


def test_llm_provider_codex_backend_uses_search_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("")
    monkeypatch.setattr("app.research_providers.find_codex_bin", lambda *_a, **_k: fake_codex)
    calls: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps({"cases": [{"url": "https://a.com/1", "title": "T"}]}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    p = LlmWebSearchProvider(settings(tmp_path), VI, codex_runner=runner)
    cases = p.collect_cases()
    assert 'web_search="live"' in calls[0] and "--search" not in calls[0] and "read-only" in calls[0]
    assert len(cases) == 1 and cases[0].views is None


# --- 第三方與模擬 ---


def test_thirdparty_disabled_without_token_and_maps_items(tmp_path: Path) -> None:
    assert not ThirdPartyApiProvider(settings(tmp_path), VI).availability().ok

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=[
            {"webVideoUrl": "https://tiktok.com/@a/video/1", "text": "T", "authorMeta": {"name": "a", "fans": 100}, "playCount": 10, "diggCount": 1, "commentCount": 0, "shareCount": 2},
            {"text": "沒有網址"},
        ])  # fmt: skip

    p = ThirdPartyApiProvider(settings(tmp_path, APIFY_TOKEN="tok"), VI, transport=httpx.MockTransport(handler))
    cases = p.collect_cases()
    assert len(cases) == 1 and cases[0].comments == 0 and cases[0].creator == "a"
    assert not p.verified


def test_mock_provider_flags_everything_as_mock() -> None:
    cases = MockSourceProvider().collect_cases(max_cases=5)
    assert cases and all(c.is_mock and c.evidence_level == EvidenceLevel.MOCK_FIXTURE and c.provider == "mock" for c in cases)
    assert all(c.title for c in cases)


# --- 適配度 ---


def test_detect_language() -> None:
    assert detect_language("Cuộc sống ở Đài Loan thật thú vị") == "vi"
    assert detect_language("ร้านอาหารไทยใกล้ตลาดนัด") == "th"
    assert detect_language("台灣超商的五個功能") == "zh"
    assert detect_language("How to live in Taiwan, tips for you") == "en"
    assert detect_language("Cara kerja di Taiwan yang aman") == "id"
    assert detect_language("") == "unknown"


def test_fit_matching_case_scores_high_with_reason() -> None:
    f = score_case(case(title_zh="越南人常漏掉的 5 個超商功能"), VI)
    assert f.score >= 80
    assert "語言與發布語言相符" in f.reason and "命中關鍵字" in f.reason and "提到受眾所在地" in f.reason
    assert f.flag == "" and f.recommended_slot == ContentSlot.NOON_1130


def test_fit_language_mismatch_is_low() -> None:
    thai = case(title="ร้านอาหารไทย 5 ร้านใกล้ตลาดนัดไทเป", narrative_category="food_guide")
    assert score_case(thai, VI).score < 60
    assert score_case(thai, profile_from_template("th-tw")).score > score_case(thai, VI).score + 25


def test_fit_keyword_hit_raises_score() -> None:
    plain = case(title="Ngày làm việc của mình", narrative_category="general")
    kw = case(title="Cuộc sống ở Đài Loan của mình", narrative_category="general")
    f_kw = score_case(kw, VI)
    assert f_kw.score > score_case(plain, VI).score
    assert "命中關鍵字" in f_kw.reason


def test_fit_compliance_and_ad_flags() -> None:
    visa = case(title="Gia hạn ARC không qua môi giới ở Đài Loan", narrative_category="practical_checklist")
    assert score_case(visa, VI).flag == FLAG_COMPLIANCE
    # 單字邊界：search / march 不能誤判成 ARC
    assert score_case(case(title="Search the march of Đài Loan"), VI).flag == ""
    ad = case(source_type=SourceType.AD, evidence_level=EvidenceLevel.AD_LIBRARY)
    f = score_case(ad, VI)
    assert f.flag == FLAG_AD and f.score <= 35


def test_fit_missing_metrics_are_neutral_not_zero() -> None:
    full = score_case(case(), VI)
    none = score_case(case(views=None, likes=None, comments=None, shares=None, evidence_level=EvidenceLevel.UNVERIFIED, confidence=0.4), VI)
    assert none.parts["engagement"] > 0
    assert "互動數據不足" in none.reason
    assert full.parts["engagement"] > none.parts["engagement"]


def test_fit_recommended_slot_rules() -> None:
    assert score_case(case(narrative_category="emotional_story"), VI).recommended_slot == ContentSlot.MORNING_0700
    assert score_case(case(narrative_category="food_guide"), VI).recommended_slot == ContentSlot.NOON_1130
    assert score_case(case(title_zh="用手機拍出好看的照片"), VI).recommended_slot == ContentSlot.EVENING_1800


# --- 研究流程與選題 ---


def test_run_research_writes_cases_and_fits_and_refuses_empty(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "r.db")
    summary = run_research(repo, account_id="carry-vietnam-tw", market=VI, provider=MockSourceProvider(), max_cases=8)
    assert "模擬資料" in summary
    assert len(repo.list_research_cases()) == 8
    assert len(repo.list_case_fits("carry-vietnam-tw")) == 8

    class Empty(MockSourceProvider):
        def collect_cases(self, *, max_cases: int = 35) -> list[ResearchCase]:
            return []

    with pytest.raises(RuntimeError, match="沒有找到"):
        run_research(repo, account_id="carry-vietnam-tw", market=VI, provider=Empty(), max_cases=5)


def test_topic_from_case_dedups_per_account(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "t.db")
    c = case()
    repo.upsert_research_case(c)
    fit = score_case(c, VI)
    topic, err = topic_from_case(repo, account_id="a1", market=VI, case=c, fit=fit)
    assert topic is not None and not err
    again, err2 = topic_from_case(repo, account_id="a1", market=VI, case=c, fit=fit)
    assert again is not None and again.topic_id == topic.topic_id
    # 另一個帳號不受影響
    other, _ = topic_from_case(repo, account_id="a2", market=VI, case=c, fit=fit)
    assert other is not None
    # 同名但不同案例，在同一帳號 90 天內會被擋下
    c2 = case(case_id="c2", source_url="https://www.tiktok.com/@b/photo/2")
    repo.upsert_research_case(c2)
    blocked, reason = topic_from_case(repo, account_id="a1", market=VI, case=c2, fit=score_case(c2, VI))
    assert blocked is None and "90 天" in reason
    no_title, reason2 = topic_from_case(repo, account_id="a1", market=VI, case=case(case_id="c3", source_url="https://x.com/3", title="", title_zh=""), fit=fit)
    assert no_title is None and "沒有標題" in reason2


# --- 路由 ---


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    monkeypatch.setattr("app.research_providers.find_codex_bin", lambda *_a, **_k: None)
    app = create_app(tmp_path, settings(tmp_path), allowed_hosts={"testserver"})
    return TestClient(app), {"X-AutoTok-Token": app.state.csrf_token}


def _wait_job(c: TestClient) -> None:
    jobs = c.app.state.jobs  # type: ignore[attr-defined]
    for _ in range(50):
        if jobs.current() is None:
            return
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_research_page_empty_state_and_disabled_sources(client: tuple[TestClient, dict[str, str]]) -> None:
    c, _ = client
    r = c.get("/research")
    assert r.status_code == 200
    assert "還沒有研究資料" in r.text
    assert 'aria-disabled="true"' in r.text  # 網路搜尋與第三方 API 沒有金鑰時停用
    assert "ANTHROPIC_API_KEY" in r.text


def test_research_run_requires_token_and_rejects_unavailable_source(client: tuple[TestClient, dict[str, str]]) -> None:
    c, h = client
    assert c.post("/research/run", data={"source": "mock"}).status_code == 403
    r = c.post("/research/run", data={"source": "web"}, headers=h)
    assert "ANTHROPIC_API_KEY" in r.headers["hx-trigger"] or "\\u" in r.headers["hx-trigger"]
    assert c.app.state.repo.last_job(account_id="carry-vietnam-tw", job="research") is None  # type: ignore[attr-defined]


def test_research_mock_run_import_filters_and_send(client: tuple[TestClient, dict[str, str]]) -> None:
    c, h = client
    assert c.post("/research/run", data={"source": "mock"}, headers=h).status_code == 200
    _wait_job(c)
    repo = c.app.state.repo  # type: ignore[attr-defined]
    last = repo.last_job(account_id="carry-vietnam-tw", job="research", only_ok=True)
    assert last and "模擬資料" in last["summary"]

    assert '<tr class="clickable"' not in c.get("/research").text  # 預設隱藏模擬資料
    mock_page = c.get("/research?ev=mock&fit=0").text
    assert mock_page.count("<tr class=\"clickable\"") == 35 and "模擬" in mock_page

    r = c.post("/research/import", data={"text": "https://www.tiktok.com/@a/photo/1\nnot-a-url"}, headers=h)
    assert r.status_code == 200 and "已開始匯入 1 筆" in r.headers["hx-trigger"].encode().decode("unicode_escape")
    _wait_job(c)
    page = c.get("/research?ev=import&fit=0").text
    assert page.count('<tr class="clickable"') == 1 and "手動匯入" in page and "缺：觀看" in page

    assert "沒有找到任何有效" in c.post("/research/import", data={"text": "no urls here"}, headers=h).headers["hx-trigger"].encode().decode("unicode_escape")

    imported = next(x for x in repo.list_research_cases() if x.provider == "manual_import")
    r = c.post("/research/send", data={"case_id": imported.case_id}, headers=h)
    assert "沒有標題" in r.headers["hx-trigger"].encode().decode("unicode_escape")  # URL-only 沒有標題，不建立主題

    mock_case = next(x for x in repo.list_research_cases() if x.provider == "mock")
    r = c.post("/research/send", data={"case_id": mock_case.case_id}, headers=h)
    assert r.headers["hx-redirect"] == f"/studio?topic=topic-carry-vietnam-tw-{mock_case.case_id}"
    topics, _ = repo.get_recent_topics_and_urls(account_id="carry-vietnam-tw")
    assert f"topic-carry-vietnam-tw-{mock_case.case_id}" in {t for t, _ in topics}
    assert repo.get_recent_topics_and_urls(account_id="someone-else")[0] == []


def test_parse_count_never_raises_on_extreme_values() -> None:
    from app.research_providers import parse_count

    assert parse_count(float("inf")) is None and parse_count(float("nan")) is None
    assert parse_count("1e400") is None and parse_count(10**15) is None
    assert parse_count("9999999999999M") is None
    assert parse_count("1.2M") == 1_200_000 and parse_count("3.5萬") == 35_000


def test_import_limits_and_bad_input(client) -> None:  # noqa: ANN001
    c, h = client
    too_many = "\n".join(f"https://example.com/{i}" for i in range(501))
    r = c.post("/research/import", data={"text": too_many}, headers=h)
    assert "500" in r.headers["hx-trigger"].encode().decode("unicode_escape")
    big = "x" * (2 * 1024 * 1024 + 10)
    r = c.post("/research/import", files={"file": ("big.csv", big.encode(), "text/csv")}, headers=h)
    assert r.status_code == 200 and "2 MB" in r.headers["hx-trigger"].encode().decode("unicode_escape")
    r = c.post("/research/import", data={"text": '[{"url": "https://e.com/1", "views": Infinity}]'}, headers=h)
    assert r.status_code == 200  # 不得 500
    r = c.post("/research/import", data={"text": "url,views\nhttps://e.com/2," + "9" * 400}, headers=h)
    assert r.status_code == 200
    r = c.post("/research/import", data={"text": "url,title\n\"unterminated"}, headers=h)
    assert r.status_code == 200


def test_upsert_keeps_existing_numbers_when_later_data_is_thinner(tmp_path: Path) -> None:
    from app.models import ResearchCase
    from app.state import PostStateRepository

    repo = PostStateRepository(tmp_path / "m.db")
    url = "https://www.tiktok.com/@a/photo/9"
    full = ResearchCase(
        case_id="c9", source_url=url, collected_at="2026-10-20T00:00:00+00:00", views=1000, likes=50, comments=5, shares=2,
        evidence_level=EvidenceLevel.MANUAL_IMPORT, confidence=0.6, provider="manual_import", title="原標題",
    )
    repo.upsert_research_case(full)
    thin = ResearchCase(
        case_id="c9", source_url=url, collected_at="2026-10-21T00:00:00+00:00", views=None, likes=60, comments=None, shares=None,
        missing_fields=["views", "comments", "shares", "follower_count"],
        evidence_level=EvidenceLevel.UNVERIFIED, confidence=0.4, provider="web_search", title="新標題",
    )
    repo.upsert_research_case(thin)
    got = next(x for x in repo.list_research_cases(days=None) if x.source_url == url)
    assert (got.views, got.likes, got.comments, got.shares) == (1000, 60, 5, 2)  # 非空的新值覆蓋，缺值保留舊值
    assert got.missing_fields == ["follower_count"]
    assert got.evidence_level == EvidenceLevel.MANUAL_IMPORT and got.confidence == 0.6  # 保留了舊數字，證據取較高者
    assert got.title == "新標題"
