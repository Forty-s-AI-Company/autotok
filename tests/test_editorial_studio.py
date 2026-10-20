"""文案草稿、provider、含草稿的製作流程與製作頁路由。不呼叫真實 LLM 或 Codex，也不讀 .env。"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.editorial as editorial
import app.production as production
from app.config import AppSettings
from app.editorial import (
    AnthropicEditorialProvider,
    CodexCliEditorialProvider,
    EditorialDraft,
    EditorialProvider,
    EditorialRejectedError,
    EditorialRequest,
    EditorialUnavailableError,
    TopicBrief,
    extract_json,
    get_editorial_provider,
)
from app.errors import AutotokSystemError
from app.markets import MarketProfile, profile_from_template
from app.models import ContentSlot, PostStatus
from app.production import produce_carousel_with_qa_retry
from app.web.main import create_app


def make_draft_dict(n: int = 4, *, extra_text: str = "") -> dict[str, object]:
    slides = [
        {
            "title": f"Tiêu đề trang {i}",
            "body": f"Nội dung chi tiết số {i} {extra_text}".strip(),
            "zh_tw": f"重點{i}：第 {i} 頁的說明",
            "scene_prompt": f"A realistic scene number {i}",
        }
        for i in range(1, n + 1)
    ]
    return {
        "title": "Năm điều cần biết",
        "zh_tw_title": "五件要知道的事",
        "caption": "Nội dung bài đăng",
        "zh_tw_caption": "貼文內容",
        "hashtags": ["#a", "#b", "#c", "#d", "#e"],
        "core_angle": "實用清單",
        "cta": "Lưu lại nhé",
        "risk_flags": [],
        "slides": slides,
    }


def make_request(market: MarketProfile | None = None, slide_count: int | None = None) -> EditorialRequest:
    return EditorialRequest(
        topic=TopicBrief(title="測試主題"),
        market=market or profile_from_template("vi-tw"),
        track_name="生活與文化",
        account_handle="@t.account",
        brand_label="Test Brand",
        slide_count=slide_count,
    )


class ScriptedProvider(EditorialProvider):
    name = "scripted"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def is_available(self) -> tuple[bool, str]:
        return True, "scripted"

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs.pop(0)


# ---------------------------------------------------------------- 草稿驗證


def test_draft_validates_slide_count_and_hashtags() -> None:
    assert len(EditorialDraft.model_validate(make_draft_dict(3)).slides) == 3
    with pytest.raises(ValidationError):
        EditorialDraft.model_validate(make_draft_dict(2))
    with pytest.raises(ValidationError):
        EditorialDraft.model_validate(make_draft_dict(11))
    bad = make_draft_dict(4)
    bad["hashtags"] = ["#a", "#b"]
    with pytest.raises(ValidationError):
        EditorialDraft.model_validate(bad)
    ok = make_draft_dict(4)
    ok["hashtags"] = ["a", "b", "c", "d", "e"]
    assert EditorialDraft.model_validate(ok).hashtags[0] == "#a"


def test_extract_json_tolerates_fences_and_noise() -> None:
    text = 'log line\n```json\n{"a": {"b": "}"}, "c": 1}\n```\ntrailing'
    assert extract_json(text) == {"a": {"b": "}"}, "c": 1}
    with pytest.raises(ValueError):
        extract_json("no json here")


# ---------------------------------------------------------------- provider 解析、重試、合規


def test_provider_retries_after_invalid_output_then_succeeds() -> None:
    provider = ScriptedProvider(["不是 JSON", json.dumps(make_draft_dict(4))])
    draft = provider.generate(make_request())
    assert len(draft.slides) == 4
    assert len(provider.prompts) == 2
    assert "輸出無法解析" in provider.prompts[1]


def test_provider_gives_up_after_two_retries() -> None:
    provider = ScriptedProvider(["x", "y", "z"])
    with pytest.raises(EditorialRejectedError):
        provider.generate(make_request())
    assert len(provider.prompts) == 3


def test_provider_enforces_requested_slide_count() -> None:
    provider = ScriptedProvider([json.dumps(make_draft_dict(4)), json.dumps(make_draft_dict(5))])
    draft = provider.generate(make_request(slide_count=5))
    assert len(draft.slides) == 5
    assert "必須正好 5 張" in provider.prompts[1]


def test_severe_claim_is_rewritten_once_then_accepted() -> None:
    bad = make_draft_dict(4, extra_text="保證高薪")
    provider = ScriptedProvider([json.dumps(bad), json.dumps(make_draft_dict(4))])
    draft = provider.generate(make_request())
    assert draft.risk_flags == []
    assert "合規檢查未通過" in provider.prompts[1]


def test_severe_claim_twice_is_rejected() -> None:
    bad = json.dumps(make_draft_dict(4, extra_text="保證高薪"))
    provider = ScriptedProvider([bad, bad])
    with pytest.raises(EditorialRejectedError) as info:
        provider.generate(make_request())
    assert info.value.reasons


def test_sensitive_topic_gets_risk_flag_but_is_not_rejected() -> None:
    draft_dict = make_draft_dict(4, extra_text="居留證延期要看官方公告")
    draft = ScriptedProvider([json.dumps(draft_dict)]).generate(make_request())
    assert editorial.SENSITIVE_FLAG in draft.risk_flags


def test_codex_provider_uses_runner_and_never_real_codex(tmp_path: Path) -> None:
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    seen: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="noise\n" + json.dumps(make_draft_dict(4)), stderr="")

    provider = CodexCliEditorialProvider(codex_bin=str(fake_bin), model="m", workdir=tmp_path, runner=runner)
    draft = provider.generate(make_request())
    assert len(draft.slides) == 4
    assert seen[0][0] == str(fake_bin) and "read-only" in seen[0]


def test_codex_provider_failure_is_reported(tmp_path: Path) -> None:
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("")

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")

    provider = CodexCliEditorialProvider(codex_bin=str(fake_bin), runner=runner)
    with pytest.raises(AutotokSystemError) as info:
        provider.generate(make_request())
    assert info.value.code == "CODEX_EXEC_FAILED"


def test_anthropic_provider_with_mock_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k"
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-5-5"
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(make_draft_dict(4))}]})

    provider = AnthropicEditorialProvider(api_key="k", transport=httpx.MockTransport(handler))
    assert len(provider.generate(make_request()).slides) == 4

    unauthorized = AnthropicEditorialProvider(api_key="bad", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(EditorialUnavailableError):
        unauthorized.generate(make_request())


def test_no_provider_available_gives_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editorial, "find_codex_bin", lambda explicit="": None)
    settings = AppSettings(_env_file=None, ANTHROPIC_API_KEY="")
    with pytest.raises(EditorialUnavailableError) as info:
        get_editorial_provider(settings)
    assert "ANTHROPIC_API_KEY" in str(info.value) and "Codex" in str(info.value)


# ---------------------------------------------------------------- 含草稿的製作


def test_produce_with_draft_uses_draft_content(tmp_path: Path) -> None:
    draft = EditorialDraft.model_validate(make_draft_dict(4))
    market = profile_from_template("vi-tw")
    pkg, report, zh_dir = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="post-draft-1",
        scheduled_date_str="2026-10-22",
        output_base_dir=tmp_path,
        account_id="t-account",
        account_handle="@t.account",
        draft=draft,
        market=market,
        brand_label="Test Brand",
    )
    assert report.passed and pkg.status == PostStatus.READY
    assert pkg.slide_count == 4 and len(list(zh_dir.glob("slide_*.jpg"))) == 4
    assert pkg.vi_title == "Năm điều cần biết" and pkg.core_angle == "實用清單"
    assert pkg.hashtags == draft.hashtags
    assert pkg.scheduled_at_taipei == "2026-10-22T11:30:00+08:00"
    assert "Tiêu đề trang 1" in pkg.slides[0].vi_text


def test_produce_with_draft_uses_market_timezone(tmp_path: Path) -> None:
    market = profile_from_template("th-tw").model_copy(update={"timezone": "Asia/Bangkok", "script": "latin"})
    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.MORNING_0700,
        post_id="post-draft-tz",
        scheduled_date_str="2026-10-22",
        output_base_dir=tmp_path,
        draft=EditorialDraft.model_validate(make_draft_dict(3)),
        market=market,
    )
    assert pkg.scheduled_at_taipei == "2026-10-22T08:00:00+07:00"


def test_missing_script_font_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(production.SCRIPT_FONT_CANDIDATES, "thai", ("泰文", ["/nonexistent/Thai.ttf"]))
    with pytest.raises(AutotokSystemError) as info:
        production.load_script_font(20, "thai")
    assert info.value.code == "FONT_NOT_FOUND" and "泰文" in str(info.value)


def test_legacy_sample_path_unchanged(tmp_path: Path) -> None:
    pkg, report, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="post-legacy",
        scheduled_date_str="2026-10-22",
        output_base_dir=tmp_path,
    )
    assert report.passed and pkg.slide_count == 6 and pkg.is_mock


# ---------------------------------------------------------------- 製作頁


class FakeProvider(EditorialProvider):
    name = "fake"

    def __init__(self, n: int = 4) -> None:
        self.n = n

    def is_available(self) -> tuple[bool, str]:
        return True, "fake"

    def complete(self, prompt: str) -> str:
        return json.dumps(make_draft_dict(self.n))


@pytest.fixture()
def client(tmp_path: Path):
    settings = AppSettings(_env_file=None, SQLITE_DB_PATH=tmp_path / "s.db", NOTION_TOKEN="", ANTHROPIC_API_KEY="")
    app = create_app(tmp_path, settings, allowed_hosts={"testserver"})
    app.state.editorial_provider = FakeProvider()
    return TestClient(app), app


def wait_idle(app, timeout: float = 90) -> None:
    end = time.time() + timeout
    while app.state.jobs.current() is not None:
        assert time.time() < end, "背景工作逾時"
        time.sleep(0.2)


def toast_of(resp) -> dict[str, str]:
    return json.loads(resp.headers["HX-Trigger"])["toast"]


def test_studio_requires_token_for_writes(client) -> None:
    c, _ = client
    r = c.post("/studio/produce", data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "topic_title": "x"})
    assert r.status_code == 403


def test_studio_empty_page_renders_and_validates_input(client) -> None:
    c, app = client
    assert c.get("/studio").status_code == 200
    headers = {"X-AutoTok-Token": app.state.csrf_token}
    r = c.post("/studio/produce", headers=headers, data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "engine": "procedural"})
    assert toast_of(r)["kind"] == "err" and "主題" in toast_of(r)["msg"]
    r = c.post("/studio/produce", headers=headers, data={"date": "bad", "slot": ContentSlot.NOON_1130.value, "topic_title": "x"})
    assert toast_of(r)["kind"] == "err"


def test_studio_produce_job_creates_post_and_review_page(client, tmp_path: Path) -> None:
    c, app = client
    headers = {"X-AutoTok-Token": app.state.csrf_token}
    r = c.post(
        "/studio/produce",
        headers=headers,
        data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "engine": "procedural", "count": "auto", "topic_title": "測試主題", "points": "一\n二"},
    )
    assert toast_of(r)["kind"] == "ok", r.headers
    wait_idle(app)

    repo = app.state.repo
    posts = repo.list_posts(account_id="carry-vietnam-tw")
    assert len(posts) == 1 and posts[0].slide_count == 4 and posts[0].status == PostStatus.READY
    job = repo.last_job(account_id="carry-vietnam-tw", job="produce")
    assert job is not None and job["status"] == "ok"
    assert (tmp_path / "artifacts/daily_production/2026-10-22" / posts[0].post_id / "draft.json").is_file()

    page = c.get(f"/studio?post={posts[0].post_id}")
    assert page.status_code == 200
    assert "品質檢查" in page.text and "全部通過" in page.text
    assert "/media/daily_production/2026-10-22/" in page.text
    assert c.get(page.text.split('"target_img": "')[1].split('"')[0]).status_code == 200


def test_studio_job_failure_is_logged(client) -> None:
    c, app = client

    class Broken(FakeProvider):
        def complete(self, prompt: str) -> str:
            return "never json"

    app.state.editorial_provider = Broken()
    headers = {"X-AutoTok-Token": app.state.csrf_token}
    c.post("/studio/produce", headers=headers, data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "engine": "procedural", "topic_title": "x"})
    wait_idle(app)
    job = app.state.repo.last_job(account_id="carry-vietnam-tw", job="produce")
    assert job["status"] == "error" and "無法解析" in job["error"]
    assert app.state.repo.list_posts(account_id="carry-vietnam-tw") == []


def test_studio_sample_engine_marks_mock(client) -> None:
    c, app = client
    headers = {"X-AutoTok-Token": app.state.csrf_token}
    c.post("/studio/produce", headers=headers, data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "engine": "sample", "count": "4", "topic_title": "範例"})
    wait_idle(app)
    posts = app.state.repo.list_posts(account_id="carry-vietnam-tw")
    assert len(posts) == 1 and posts[0].is_mock
    assert "模擬" in c.get(f"/studio?post={posts[0].post_id}").text


def test_notion_sync_requires_binding_and_logs_job(client, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.web.routes.studio as studio_routes

    c, app = client
    headers = {"X-AutoTok-Token": app.state.csrf_token}
    c.post("/studio/produce", headers=headers, data={"date": "2026-10-22", "slot": ContentSlot.NOON_1130.value, "engine": "procedural", "topic_title": "x"})
    wait_idle(app)
    post_id = app.state.repo.list_posts(account_id="carry-vietnam-tw")[0].post_id

    class Unbound:
        @staticmethod
        def for_account(repo, settings, account_id, *, transport=None):
            return None

    monkeypatch.setattr(studio_routes, "NotionSyncService", Unbound)
    r = c.post("/studio/sync", headers=headers, data={"post_id": post_id})
    assert toast_of(r)["kind"] == "err" and "尚未綁定" in toast_of(r)["msg"]

    class Result:
        notion_page_url = "https://notion.so/page-1"

    class Service:
        def sync_post_to_notion(self, package, *, zh_media_dir=None):
            return Result()

    class Bound:
        @staticmethod
        def for_account(repo, settings, account_id, *, transport=None):
            return Service()

    monkeypatch.setattr(studio_routes, "NotionSyncService", Bound)
    r = c.post("/studio/sync", headers=headers, data={"post_id": post_id})
    assert toast_of(r)["kind"] == "ok"
    wait_idle(app)
    job = app.state.repo.last_job(account_id="carry-vietnam-tw", job="notion_sync")
    assert job["label"] == post_id and job["status"] == "ok"
