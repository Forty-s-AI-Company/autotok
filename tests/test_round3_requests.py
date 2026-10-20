"""第三輪需求：進度提示、市場設定唯讀欄位、研究訴求、引擎掃描與模型選擇。"""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.editorial import EditorialRequest, TopicBrief, build_prompt
from app.engines import EngineError, parse_model_catalog, scan_codex
from app.markets import profile_from_template
from app.models import ContentSlot
from app.production import produce_carousel_with_qa_retry
from app.web.main import create_app

CATALOG = json.dumps(
    {
        "models": [
            {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 2, "description": "最強"},
            {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list", "priority": 4, "description": "主力"},
            {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "priority": 5, "description": "隱藏"},
        ]
    }
)


def _runner(stdout: str = CATALOG, rc: int = 0):
    return lambda cmd: subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="boom")


@pytest.fixture()
def fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CODEX_BIN", str(path))
    return path


def _client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"), allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    c.app_ref = app  # type: ignore[attr-defined]
    return c


def _wait(app) -> None:  # noqa: ANN001
    for _ in range(100):
        if app.state.jobs.current() is None:
            return
        threading.Event().wait(0.05)


# ---------- 模型清單與掃描 ----------


def test_parse_catalog_orders_by_priority_and_marks_hidden() -> None:
    models = parse_model_catalog(CATALOG)
    assert [m.slug for m in models] == ["gpt-6-astra", "gpt-6-sol", "gpt-reserve"]
    assert [m.listed for m in models] == [True, True, False]
    with pytest.raises(EngineError):
        parse_model_catalog("not json")


def test_scan_reports_valid_missing_hidden_and_unreadable(fake_codex: Path) -> None:
    assert scan_codex("", "gpt-6-sol", runner=_runner()).level == "ok"
    gone = scan_codex("", "gpt-old", runner=_runner())
    assert gone.level == "err" and "已不在" in gone.message
    assert scan_codex("", "gpt-reserve", runner=_runner()).level == "warn"
    unreadable = scan_codex("", "gpt-6-sol", runner=_runner(rc=1))
    assert unreadable.level == "warn" and "不能確認" in unreadable.message


def test_scan_without_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.engines.find_codex_bin", lambda explicit="": None)
    assert scan_codex("", "x", runner=_runner()).level == "err"


def test_model_selection_persists_and_rejects_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path) -> None:
    import app.web.routes.engines as eng

    monkeypatch.setattr(eng, "list_codex_models", lambda explicit="": parse_model_catalog(CATALOG))
    monkeypatch.setattr(eng, "scan_codex", lambda b, m: scan_codex(b, m, runner=_runner()))
    c = _client(tmp_path)
    ok = c.post("/engines/model", data={"model": "gpt-6-astra"})
    assert "ok" in ok.headers["HX-Trigger"] and c.app_ref.state.settings.codex_model == "gpt-6-astra"  # type: ignore[attr-defined]
    assert c.app_ref.state.repo.get_app_setting("codex_model") == "gpt-6-astra"  # type: ignore[attr-defined]
    bad = c.post("/engines/model", data={"model": "nope"})
    assert "err" in bad.headers["HX-Trigger"] and c.app_ref.state.settings.codex_model == "gpt-6-astra"  # type: ignore[attr-defined]
    # 重新啟動後沿用上次的選擇
    again = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"), allowed_hosts={"testserver"})
    assert again.state.settings.codex_model == "gpt-6-astra"
    panel = TestClient(again).get("/engines/panel").text
    assert "GPT-6-Astra" in panel and "GPT-Reserve" not in panel


def test_scan_is_logged_and_shown_in_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path) -> None:
    import app.web.routes.engines as eng

    monkeypatch.setattr(eng, "scan_codex", lambda b, m: scan_codex(b, m, runner=_runner()))
    c = _client(tmp_path)
    assert c.post("/engines/scan").status_code == 200
    row = c.app_ref.state.repo.last_job(account_id="", job="engine_check")  # type: ignore[attr-defined]
    assert row["status"] == "ok" and row["summary"].startswith("ok|")
    assert "掃描於" in c.get("/engines/panel").text


def test_scan_failure_is_an_error_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_codex: Path) -> None:
    import app.web.routes.engines as eng

    monkeypatch.setattr(eng, "scan_codex", lambda b, m: scan_codex("", "gpt-old", runner=_runner()))
    c = _client(tmp_path)
    c.post("/engines/scan")
    row = c.app_ref.state.repo.last_job(account_id="", job="engine_check")  # type: ignore[attr-defined]
    assert row["status"] == "error" and "已不在" in row["error"]
    assert "失效" in c.get("/engines/panel").text


# ---------- 今天頁提示、市場設定、研究訴求 ----------


def test_today_progress_has_tooltips(tmp_path: Path) -> None:
    c = _client(tmp_path)
    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130, post_id="post-2026-10-21-1130", scheduled_date_str="2026-10-21", output_base_dir=tmp_path / "artifacts" / "d"
    )
    c.app_ref.state.repo.upsert_post(pkg)  # type: ignore[attr-defined]
    html = c.get("/?date=2026-10-21").text
    assert 'data-tip="選題：已完成"' in html and 'data-tip="排程：尚未開始"' in html


def test_display_name_is_read_only_and_brief_is_saved(tmp_path: Path) -> None:
    c = _client(tmp_path)
    repo = c.app_ref.state.repo  # type: ignore[attr-defined]
    before = repo.get_account("carry-vietnam-tw").display_name
    html = c.get("/settings").text
    assert 'id="mdn"' not in html and 'name="content_brief"' in html
    r = c.post("/settings/market", data={"account_id": "carry-vietnam-tw", "display_name": "被改掉", "content_brief": "要有美食與想家內容"})
    assert r.status_code == 200
    assert repo.get_account("carry-vietnam-tw").display_name == before
    assert repo.get_market_profile("carry-vietnam-tw").content_brief == "要有美食與想家內容"


def test_research_page_prefills_brief_and_run_uses_override(tmp_path: Path) -> None:
    c = _client(tmp_path)
    repo = c.app_ref.state.repo  # type: ignore[attr-defined]
    m = repo.get_market_profile("carry-vietnam-tw")
    m.content_brief = "預設訴求ABC"
    repo.save_market_profile("carry-vietnam-tw", m)
    page = c.get("/research").text
    assert 'id="resBrief"' in page and "預設訴求ABC" in page

    r = c.post("/research/run", data={"source": "mock", "brief": "這次只研究美食"})
    assert "ok" in r.headers["HX-Trigger"]
    _wait(c.app_ref)  # type: ignore[attr-defined]
    last = repo.last_job(account_id="carry-vietnam-tw", job="research")
    assert last["status"] == "ok" and "訴求：這次只研究美食" in last["summary"]
    assert repo.get_market_profile("carry-vietnam-tw").content_brief == "預設訴求ABC"  # 只影響這一次

    c.post("/research/brief/save", data={"brief": "改成新的預設"})
    assert repo.get_market_profile("carry-vietnam-tw").content_brief == "改成新的預設"


def test_brief_reaches_research_and_editorial_prompts() -> None:
    from app.research_providers import build_search_prompt

    market = profile_from_template("vi-tw")
    market.content_brief = "美食、文化、想家，有些要 CTA"
    assert "美食、文化、想家，有些要 CTA" in build_search_prompt(market, 10)
    req = EditorialRequest(topic=TopicBrief(title="t"), market=market, track_name="生活", account_handle="@a", brand_label="b", slide_count=None)
    assert "美食、文化、想家，有些要 CTA" in build_prompt(req)


def test_long_lists_scroll_and_edit_link_has_jump_target(tmp_path: Path) -> None:
    css = (Path(__file__).resolve().parent.parent / "app/web/static/app.css").read_text(encoding="utf-8")
    assert ".list-sel { max-height: 320px; overflow-y: auto; }" in css
    html = _client(tmp_path).get("/settings").text
    assert 'data-jump="market"' in html and 'id="market"' in html


def test_research_page_lists_low_fit_cases_by_default(tmp_path: Path) -> None:
    from app.models import EvidenceLevel, ResearchCase

    c = _client(tmp_path)
    repo = c.app_ref.state.repo  # type: ignore[attr-defined]
    repo.upsert_research_case(
        ResearchCase(
            case_id="case-low", source_url="https://example.com/v/1", collected_at="2026-10-20T10:00:00+00:00",
            title="適配度很低的案例標題", evidence_level=EvidenceLevel.PARTIAL_PUBLIC, provider="web_search",
        )
    )
    html = c.get("/research").text
    assert "適配度很低的案例標題" in html and "1 / 1" in html
    hidden = c.get("/research?fit=80").text
    assert "適配度很低的案例標題" not in hidden and "顯示全部 1 筆" in hidden
