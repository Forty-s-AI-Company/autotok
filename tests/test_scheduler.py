"""排程器：冪等、時區、退避與通知去重、鎖、發布閘門。全部使用假的執行函式，不碰真實服務。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.scheduler as sched
from app.config import AppSettings
from app.markets import SLOT_KINDS
from app.models import ContentSlot, PostStatus, TikTokAccountProfile
from app.pipeline import make_post_id, pick_topic
from app.production import produce_carousel_with_qa_retry
from app.publisher import PublishOutcome
from app.scheduler import Executors, SchedulerBusyError, acquire_lock, plan, tick
from app.state import PostStateRepository
from app.web.main import create_app

NOW = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)  # 台北 20:30


class Calls:
    def __init__(self) -> None:
        self.research = 0
        self.produce: list[tuple[str, str, str]] = []
        self.notion = 0
        self.publish: list[str] = []
        self.publish_outcome = PublishOutcome("exported", "已匯出發布包")
        self.produce_error: str | None = None
        self.research_error: str | None = None

    def executors(self) -> Executors:
        def research(repo, cfg, aid, market):  # noqa: ANN001
            self.research += 1
            if self.research_error:
                raise RuntimeError(self.research_error)
            return "網路搜尋：3 筆"

        def produce(repo, cfg, root, aid, date_str, slot):  # noqa: ANN001
            self.produce.append((aid, date_str, slot.value))
            if self.produce_error:
                raise RuntimeError(self.produce_error)
            return f"{make_post_id(aid, date_str, slot)}：6 張"

        def notion(repo, cfg, aid, post):  # noqa: ANN001
            self.notion += 1
            return "ok"

        def publish(repo, cfg, aid, post_id):  # noqa: ANN001
            self.publish.append(post_id)
            return self.publish_outcome

        return Executors(research, produce, notion, publish)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg = AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", NOTION_TOKEN="")
    repo = PostStateRepository(cfg.sqlite_db_path)
    market = repo.get_market_profile("carry-vietnam-tw")
    market.auto_enabled = True
    repo.save_market_profile("carry-vietnam-tw", market)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(sched, "send_notification", lambda repo, cfg, *, title, message, level="error": sent.append((title, message)) or True)
    return cfg, repo, sent, Calls(), tmp_path


def run(env, now=NOW, **kw):  # noqa: ANN001, ANN003
    cfg, repo, _, calls, root = env
    return tick(repo, cfg, root, now_utc=now, executors=calls.executors(), **kw)


def make_post(repo, root, account_id, date_str, slot, *, mock=False, status=PostStatus.READY):  # noqa: ANN001
    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=slot, post_id=make_post_id(account_id, date_str, slot), scheduled_date_str=date_str,
        output_base_dir=root / "artifacts" / "d", account_id=account_id,
    )  # fmt: skip
    pkg.is_mock = mock
    pkg.status = status
    repo.upsert_post(pkg)
    return pkg


def set_cap(repo, cap: str) -> None:  # noqa: ANN001
    repo.save_tiktok_binding("carry-vietnam-tw", token_ref="x", publish_capability=cap)


# ---------- 冪等與時間 ----------


def test_idempotent_two_ticks_do_work_once(env) -> None:  # noqa: ANN001
    _, _, sent, calls, _ = env
    r1 = run(env)
    assert calls.research == 1 and len(calls.produce) == 3 and not r1.failed
    assert {d for _, d, _ in calls.produce} == {"2026-10-21"}  # 20:30 時今天的發文時間都過了，只做明天
    run(env)
    run(env, NOW + timedelta(minutes=10))
    assert calls.research == 1 and len(calls.produce) == 3 and not sent


def test_not_due_before_times_and_timezones(env) -> None:  # noqa: ANN001
    cfg, repo, _, calls, root = env
    early = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)  # 台北 16:00：研究 17:00、準備 20:00 都還沒到
    tick(repo, cfg, root, now_utc=early, executors=calls.executors())
    assert calls.research == 0 and all(d == "2026-10-20" for _, d, _ in calls.produce)  # 只剩今天 18:00 的補做
    calls.produce.clear()
    repo.upsert_account(TikTokAccountProfile(account_id="la-acc", handle="@la.acc", display_name="x", is_active=False))
    m = repo.get_market_profile("la-acc")
    m.auto_enabled, m.timezone = True, "America/Los_Angeles"
    repo.save_market_profile("la-acc", m)
    tick(repo, cfg, root, now_utc=NOW, executors=calls.executors(), account_id="la-acc")
    assert calls.research == 0  # 洛杉磯 05:30，還沒到研究時間
    assert {d for aid, d, _ in calls.produce if aid == "la-acc"} == {"2026-10-20"}  # 今天尚未到的內容線會補做；明天的準備時間（當地 20:00）還沒到


def test_cross_midnight_catches_up_today(env) -> None:  # noqa: ANN001
    _, repo, _, calls, _ = env
    after_midnight = datetime(2026, 10, 20, 17, 30, tzinfo=UTC)  # 台北 10/21 01:30
    run(env, after_midnight)
    assert {d for _, d, _ in calls.produce} == {"2026-10-21"}  # 明天（10/22）的準備時間還沒到
    assert repo.list_jobs(account_id="carry-vietnam-tw", limit=3)


def test_disabled_account_does_nothing_and_dry_run_writes_nothing(env) -> None:  # noqa: ANN001
    cfg, repo, _, calls, root = env
    m = repo.get_market_profile("carry-vietnam-tw")
    m.auto_enabled = False
    repo.save_market_profile("carry-vietnam-tw", m)
    run(env)
    assert calls.research == 0 and calls.produce == []
    m.auto_enabled = True
    repo.save_market_profile("carry-vietnam-tw", m)
    before = len(repo.list_jobs(limit=100))
    res = tick(repo, cfg, root, now_utc=NOW, dry_run=True, executors=calls.executors())
    assert res.planned and calls.research == 0 and len(repo.list_jobs(limit=100)) == before
    assert not (cfg.sqlite_db_path.parent / "scheduler.lock").exists()


# ---------- 失敗、退避、通知去重 ----------


def test_pick_failure_notifies_once_then_backoff_and_gives_up(env) -> None:  # noqa: ANN001
    _, repo, sent, calls, _ = env
    calls.produce_error = "挑不到題目：沒有可用的研究案例"
    r = run(env)
    assert len(r.failed) == 3 and len(sent) == 3 and "挑不到題目" in sent[0][1]
    run(env, NOW + timedelta(minutes=10))  # 退避中
    assert len(calls.produce) == 3 and len(sent) == 3
    run(env, NOW + timedelta(minutes=31))  # 第 2 次，狀態沒變不重複通知
    assert len(calls.produce) == 6 and len(sent) == 3
    run(env, NOW + timedelta(minutes=62))  # 第 3 次，狀態改變（停止重試）再通知一次
    assert len(calls.produce) == 9 and len(sent) == 6 and "停止重試" in sent[-1][0]
    run(env, NOW + timedelta(minutes=93))
    assert len(calls.produce) == 9 and len(sent) == 6
    acts = plan(repo, env[0], NOW + timedelta(minutes=93))
    assert any(a.state == "gaveup" for a in acts)


def test_notification_text_is_redacted(env) -> None:  # noqa: ANN001
    _, _, sent, calls, _ = env
    calls.research_error = "access_token=SECRET123 失敗"
    run(env)
    assert sent and "SECRET123" not in sent[0][1]


# ---------- 鎖 ----------


def test_lock_blocks_concurrent_and_takes_over_stale(tmp_path: Path) -> None:
    lock = tmp_path / "s.lock"
    acquire_lock(lock, NOW)
    with pytest.raises(SchedulerBusyError):
        acquire_lock(lock, NOW + timedelta(minutes=5))
    acquire_lock(lock, NOW + timedelta(hours=3))  # 殘留鎖，接管
    assert os.path.exists(lock)


def test_tick_skips_when_locked_or_web_job_running(env) -> None:  # noqa: ANN001
    cfg, repo, _, calls, root = env
    lock = cfg.sqlite_db_path.parent / "scheduler.lock"
    acquire_lock(lock, NOW)
    assert "另一個程序" in run(env).skipped_reason and calls.research == 0
    lock.unlink()
    repo.log_job_start(account_id="carry-vietnam-tw", job="produce", label="製作 10/21")
    res = tick(repo, cfg, root, now_utc=datetime.now(UTC), executors=calls.executors())
    assert "操作台正在執行" in res.skipped_reason and calls.research == 0
    assert not lock.exists()


# ---------- 發布閘門 ----------

SLOT = ContentSlot.NOON_1130  # 11:30
NOON_UTC = datetime(2026, 10, 20, 3, 45, tzinfo=UTC)  # 台北 11:45


def _publish_env(env, cap: str, auto_publish: bool = False):  # noqa: ANN001
    _, repo, _, _, root = env
    make_post(repo, root, "carry-vietnam-tw", "2026-10-20", SLOT)
    set_cap(repo, cap)
    m = repo.get_market_profile("carry-vietnam-tw")
    m.auto_publish = auto_publish
    repo.save_market_profile("carry-vietnam-tw", m)


def test_export_only_exports_once_without_notification(env) -> None:  # noqa: ANN001
    _, _, sent, calls, _ = env
    _publish_env(env, "export_only")
    run(env, NOON_UTC)
    run(env, NOON_UTC + timedelta(minutes=10))
    assert calls.publish == ["post-2026-10-20-1130"] and not sent


def test_blocked_is_not_a_failure_and_failed_notifies(env) -> None:  # noqa: ANN001
    _, repo, sent, calls, _ = env
    _publish_env(env, "public", auto_publish=True)
    calls.publish_outcome = PublishOutcome("blocked", "App 尚未通過審核")
    run(env, NOON_UTC)
    assert calls.publish == ["post-2026-10-20-1130"] and not sent
    rows = repo.job_rows(account_id="carry-vietnam-tw", job="auto_publish", label="post-2026-10-20-1130")
    assert rows[-1]["status"] == "skipped"
    # 另一篇：發布失敗要通知
    make_post(repo, env[4], "carry-vietnam-tw", "2026-10-20", ContentSlot.MORNING_0700)
    calls.publish_outcome = PublishOutcome("failed", "網路逾時")
    run(env, datetime(2026, 10, 19, 23, 30, tzinfo=UTC) + timedelta(hours=0))  # 台北 07:30
    assert sent and "發布" in sent[0][0]


def test_publish_off_for_self_only_without_auto_publish(env) -> None:  # noqa: ANN001
    _, repo, _, calls, _ = env
    _publish_env(env, "self_only", auto_publish=False)
    run(env, NOON_UTC)
    assert calls.publish == []
    assert not any(a.kind == "publish" for a in plan(repo, env[0], NOON_UTC))


def test_missed_publish_is_logged_and_notified_once(env) -> None:  # noqa: ANN001
    _, repo, sent, calls, _ = env
    _publish_env(env, "export_only")
    late = datetime(2026, 10, 20, 6, 0, tzinfo=UTC)  # 台北 14:00，超過 11:30 + 2 小時
    run(env, late)
    run(env, late + timedelta(minutes=10))
    assert calls.publish == [] and len(sent) == 1 and "錯過" in sent[0][0]
    rows = repo.job_rows(account_id="carry-vietnam-tw", job="auto_publish", label="post-2026-10-20-1130")
    assert [r["status"] for r in rows] == ["missed"]


def test_mock_posts_are_never_published_or_synced(env) -> None:  # noqa: ANN001
    _, repo, _, calls, root = env
    make_post(repo, root, "carry-vietnam-tw", "2026-10-20", SLOT, mock=True)
    set_cap(repo, "export_only")
    run(env, NOON_UTC)
    assert calls.publish == [] and calls.notion == 0


# ---------- 設定頁、CLI、題目挑選 ----------


def test_panel_and_save_enforce_publish_capability(tmp_path: Path) -> None:
    cfg = AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", NOTION_TOKEN="")
    app = create_app(tmp_path, cfg, allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    repo = app.state.repo
    r = c.post("/schedule/save", data={"account_id": "carry-vietnam-tw", "auto_enabled": "on", "auto_publish": "on", "research_time": "16:30", "prepare_time": "21:00"})
    assert r.status_code == 200
    m = repo.get_market_profile("carry-vietnam-tw")
    assert m.auto_enabled and not m.auto_publish and (m.auto_research_time, m.auto_prepare_time) == ("16:30", "21:00")
    assert "err" in c.post("/schedule/save", data={"account_id": "carry-vietnam-tw", "research_time": "25:00"}).headers["HX-Trigger"]
    repo.save_tiktok_binding("carry-vietnam-tw", token_ref="x", publish_capability="public")
    c.post("/schedule/save", data={"account_id": "carry-vietnam-tw", "auto_enabled": "on", "auto_publish": "on"})
    assert repo.get_market_profile("carry-vietnam-tw").auto_publish
    html = c.get("/schedule/panel").text
    assert "下一步：" in html and "16:30" in html
    assert "自動排程" in c.get("/settings").text


def test_status_warns_when_tick_is_stale(tmp_path: Path) -> None:
    from app.web.status import system_status

    cfg = AppSettings(SQLITE_DB_PATH=tmp_path / "t.db", NOTION_TOKEN="")
    repo = PostStateRepository(cfg.sqlite_db_path)
    assert not any(r["name"] == "自動排程" for r in system_status(repo, cfg, "carry-vietnam-tw"))
    m = repo.get_market_profile("carry-vietnam-tw")
    m.auto_enabled = True
    repo.save_market_profile("carry-vietnam-tw", m)
    row = next(r for r in system_status(repo, cfg, "carry-vietnam-tw") if r["name"] == "自動排程")
    assert row["level"] == "warn"


def test_pick_topic_reports_why_nothing_was_found(tmp_path: Path) -> None:
    repo = PostStateRepository(tmp_path / "t.db")
    topic, why = pick_topic(repo, "carry-vietnam-tw", repo.get_market_profile("carry-vietnam-tw"), SLOT_KINDS[1])
    assert topic is None and "沒有可用的研究案例" in why


def test_cli_dry_run_lists_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from app.cli import main

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("NOTION_TOKEN", "")
    repo = PostStateRepository(tmp_path / "t.db")
    m = repo.get_market_profile("carry-vietnam-tw")
    m.auto_enabled = True
    repo.save_market_profile("carry-vietnam-tw", m)
    assert main(["scheduler", "tick", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "研究爆款案例" in out and "製作" in out
    assert not repo.list_jobs(limit=5)
