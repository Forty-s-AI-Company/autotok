"""Web 外殼：安全防護、帳號與市場設定、背景工作、各頁渲染。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.models import ContentSlot, PostStatus
from app.web.jobs import JobBusyError
from app.web.main import create_app
from app.web.routes.today import step_states


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", NOTION_TOKEN=""), allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    c.app_ref = app  # type: ignore[attr-defined]
    return c


def test_all_pages_render(client: TestClient) -> None:
    for path in ["/", "/research", "/studio", "/library", "/analytics", "/settings"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "AutoTok" in r.text


def test_rejects_non_local_host(tmp_path: Path) -> None:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"))
    c = TestClient(app, base_url="http://evil.example")
    assert c.get("/").status_code == 403


def test_post_requires_token_and_same_origin(client: TestClient) -> None:
    app = client.app_ref  # type: ignore[attr-defined]
    no_token = TestClient(app)
    assert no_token.post("/accounts/switch", data={"account_id": "x"}).status_code == 403
    r = client.post("/accounts/switch", data={"account_id": "carry-vietnam-tw"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/accounts/switch", data={"account_id": "carry-vietnam-tw"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 204


def test_media_blocks_path_traversal(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "secret.jpg").write_bytes(b"x")
    (tmp_path / "artifacts" / "ok.jpg").write_bytes(b"\xff\xd8\xff")
    assert client.get("/media/ok.jpg").status_code == 200
    assert client.get("/media/../secret.jpg").status_code == 404
    assert client.get("/media/%2e%2e/secret.jpg").status_code == 404
    (tmp_path / "artifacts" / "notes.txt").write_text("x")
    assert client.get("/media/notes.txt").status_code == 404


def test_account_create_switch_delete_and_market_save(client: TestClient) -> None:
    repo = client.app_ref.state.repo  # type: ignore[attr-defined]
    bad = client.post("/accounts/create", data={"handle": "no spaces!", "template": "th-tw"})
    assert "err" in bad.headers["HX-Trigger"]
    ok = client.post("/accounts/create", data={"handle": "@siam.in.taiwan", "display_name": "Siam", "template": "th-tw"})
    assert ok.status_code == 200
    acc = repo.get_account("siam-in-taiwan")
    assert acc is not None and acc.target_language == "th"
    assert repo.get_active_account().account_id == "siam-in-taiwan"
    assert repo.get_market_profile("siam-in-taiwan").script == "thai"

    r = client.post(
        "/settings/market",
        data={"account_id": "siam-in-taiwan", "target_language": "id", "timezone": "Asia/Jakarta", "time_07:00_EMOTION_GROWTH": "06:30",
              "compliance_notes": "a\nb", "seed_keywords": "x\n\ny"},
    )
    assert r.status_code == 200
    m = repo.get_market_profile("siam-in-taiwan")
    assert (m.target_language, m.timezone, m.script) == ("id", "Asia/Jakarta", "latin")
    assert m.time_for(ContentSlot.MORNING_0700) == "06:30"
    assert m.compliance_notes == ["a", "b"] and m.seed_keywords == ["x", "y"]

    bad_time = client.post("/settings/market", data={"account_id": "siam-in-taiwan", "time_07:00_EMOTION_GROWTH": "25:99"})
    assert "err" in bad_time.headers["HX-Trigger"]

    assert client.post("/accounts/delete", data={"account_id": "siam-in-taiwan"}).status_code == 200
    assert repo.get_account("siam-in-taiwan") is None


def test_job_runner_single_flight_and_logging(client: TestClient) -> None:
    app = client.app_ref  # type: ignore[attr-defined]
    runner, repo = app.state.jobs, app.state.repo
    gate = threading.Event()

    def slow(handle):  # noqa: ANN001
        handle.progress("token=abc123 處理中")
        gate.wait(5)
        return "完成 3 筆"

    runner.submit(account_id="carry-vietnam-tw", job="demo", label="測試工作", fn=slow)
    with pytest.raises(JobBusyError):
        runner.submit(account_id="carry-vietnam-tw", job="demo", label="第二個", fn=lambda h: "x")
    assert "測試工作" in client.get("/").text
    gate.set()
    for _ in range(50):
        if runner.current() is None:
            break
        threading.Event().wait(0.05)
    last = repo.last_job(account_id="carry-vietnam-tw", job="demo")
    assert last and last["status"] == "ok" and last["summary"] == "完成 3 筆"

    def boom(handle):  # noqa: ANN001
        raise RuntimeError("sessionid=SECRET123 失敗")

    runner.submit(account_id="carry-vietnam-tw", job="demo", label="壞掉", fn=boom, wait=True)
    bad = repo.last_job(account_id="carry-vietnam-tw", job="demo")
    assert bad and bad["status"] == "error" and "SECRET123" not in str(bad["error"])
    resp = client.get("/activity?seen=0")
    assert "jobFinished" in resp.headers.get("HX-Trigger", "")


def test_step_states() -> None:
    assert step_states(None, False) == [""] * 5

    class P:  # 最小替身
        def __init__(self, status: PostStatus) -> None:
            self.status = status

    s = step_states(P(PostStatus.QUALITY_FAILED), False)  # type: ignore[arg-type]
    assert s[0] == "d" and "f" in s
    s = step_states(P(PostStatus.READY), False)  # type: ignore[arg-type]
    assert s[:3] == ["d", "d", "d"] and s[3] == "r"


def test_analytics_hides_mock_and_says_so(client: TestClient) -> None:
    from app.analytics import build_mock_observations_fixture

    repo = client.app_ref.state.repo  # type: ignore[attr-defined]
    for o in build_mock_observations_fixture():
        repo.upsert_post_observation(o)
    html = client.get("/analytics").text
    assert "還沒有成效資料" in html and "模擬資料已隱藏" in html


def test_pages_have_no_leftover_marketing_copy(client: TestClient) -> None:
    banned = ["Phase", "免背指令", "專屬視角", "🚀", "✨", "📊"]
    for path in ["/", "/library", "/analytics", "/settings"]:
        html = client.get(path).text
        for word in banned:
            assert word not in html, (path, word)


def test_secrets_never_rendered(tmp_path: Path) -> None:
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", NOTION_TOKEN="ntn_super_secret_xyz"), allowed_hosts={"testserver"})
    c = TestClient(app)
    for path in ["/", "/settings", "/library"]:
        assert "ntn_super_secret_xyz" not in c.get(path).text


def test_today_links_to_specific_post_and_library_has_gallery(client: TestClient, tmp_path: Path) -> None:
    from app.production import produce_carousel_with_qa_retry

    for slot, pid in ((ContentSlot.MORNING_0700, "post-2026-10-21-0700"), (ContentSlot.NOON_1130, "post-2026-10-21-1130")):
        pkg, _, _ = produce_carousel_with_qa_retry(slot=slot, post_id=pid, scheduled_date_str="2026-10-21", output_base_dir=tmp_path / "artifacts" / "d")
        client.app_ref.state.repo.upsert_post(pkg)  # type: ignore[attr-defined]
    today = client.get("/?date=2026-10-21").text
    assert 'href="/library#post-2026-10-21-1130"' in today and 'href="/library#post-2026-10-21-0700"' in today
    lib = client.get("/library").text
    assert 'id="post-2026-10-21-1130"' in lib and "data-lb" in lib and "swiper-wrapper" in lib
    assert "<details class=\"panel post\" id=\"post-2026-10-21-1130\" open" not in lib  # 兩篇以上時預設全部收合，由網址 # 決定展開哪一篇
