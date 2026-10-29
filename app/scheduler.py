"""排程器：由系統排程（Windows 工作排程器／launchd）每 10 分鐘觸發一次 `tick`。

每次 tick 都是冪等的：依各帳號當地時間判斷哪些工作到期且尚未完成，只做該做的。
完成與否、失敗次數、通知紀錄全部存在 `sync_log`（job：auto_research、auto_produce、notion_sync、
auto_publish、auto_notify、scheduler_tick）。規劃函式 `plan()` 同時供 tick、`--dry-run` 與設定頁使用。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import AppSettings, redact_sensitive
from app.markets import SLOT_KINDS, MarketProfile
from app.models import CarouselPostPackage, ContentSlot, PostStatus
from app.notify import send_notification
from app.pipeline import (
    brief_from_topic,
    make_post_id,
    mark_topic_used,
    pick_topic,
    produce_post,
    sync_post_to_notion,
)
from app.publisher import PublishOutcome, publish_post, reconcile_inbox_post
from app.research_providers import LlmWebSearchProvider
from app.research_service import CASE_WINDOW_DAYS, NoCasesFoundError, run_research
from app.state import PostStateRepository

MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(minutes=30)
PUBLISH_GRACE = timedelta(hours=2)
LOCK_STALE = timedelta(hours=2)
WEB_BUSY_WINDOW = timedelta(minutes=30)
DONE_STATUSES = {"ok", "skipped", "missed"}
INBOX_WINDOW_DAYS = 7  # 收件匣草稿與已發布貼文，Notion 狀態最多追蹤幾天（使用者可能隔幾天才按發布）
NOTION_STATUSES = (PostStatus.READY, PostStatus.READY_TO_REVIEW, PostStatus.SENT_TO_INBOX, PostStatus.PUBLISHED)

KIND_JOB = {"research": "auto_research", "produce": "auto_produce", "notion": "notion_sync", "publish": "auto_publish"}
KIND_NAME = {"research": "研究", "produce": "製作", "notion": "同步 Notion", "publish": "發布"}


@dataclass
class Action:
    account_id: str
    handle: str
    kind: str  # research | produce | notion | publish
    target: str  # research：當地日期；其餘：post_id
    due_at: datetime  # 帶時區
    state: str  # due | waiting | retry | done | gaveup | missed
    text: str  # 給人看的描述
    slot: ContentSlot | None = None
    date_str: str = ""
    attempts: int = 0

    @property
    def job(self) -> str:
        return KIND_JOB[self.kind]


@dataclass
class Executors:
    """實際做事的函式；測試可換成假的。"""

    research: Callable[[PostStateRepository, AppSettings, str, MarketProfile], str]
    produce: Callable[[PostStateRepository, AppSettings, Path, str, str, ContentSlot], str]
    notion: Callable[[PostStateRepository, AppSettings, str, CarouselPostPackage], str]
    publish: Callable[[PostStateRepository, AppSettings, str, str], PublishOutcome]
    reconcile: Callable[[PostStateRepository, AppSettings, str, str], str] = reconcile_inbox_post


def _default_research(repo: PostStateRepository, cfg: AppSettings, account_id: str, market: MarketProfile) -> str:
    try:
        return run_research(repo, account_id=account_id, market=market, provider=LlmWebSearchProvider(cfg, market), max_cases=30)
    except NoCasesFoundError:
        # 沒找到新案例不是故障：沿用資料庫既有案例，不要每天失敗三次並發失敗通知
        existing = len(repo.list_research_cases(days=CASE_WINDOW_DAYS))
        return f"這次沒有找到新案例，沿用資料庫既有的 {existing} 筆"


def _default_produce(
    repo: PostStateRepository, cfg: AppSettings, root: Path, account_id: str, date_str: str, slot: ContentSlot
) -> str:
    market = repo.get_market_profile(account_id)
    topic, why = pick_topic(repo, account_id, market, slot)
    if topic is None:
        raise RuntimeError(f"挑不到題目：{why}")
    pkg = produce_post(
        repo, cfg, root, account_id=account_id, date_str=date_str, slot=slot, engine="codex", count=None,
        brief=brief_from_topic(repo, topic),
    )  # fmt: skip
    mark_topic_used(repo, account_id, topic)
    return f"{pkg.post_id}：{pkg.slide_count} 張，題目「{topic.title_zh_tw[:30]}」"


def default_executors() -> Executors:
    return Executors(_default_research, _default_produce, sync_post_to_notion, publish_post)


# ---------------------------------------------------------------------------
# 規劃
# ---------------------------------------------------------------------------


def _at(d: date, hhmm: str, tz: ZoneInfo) -> datetime:
    try:
        h, m = (int(x) for x in hhmm.split(":"))
    except ValueError:
        h, m = 0, 0
    return datetime(d.year, d.month, d.day, h, m, tzinfo=tz)


def _parse(ts: object) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _state_from_rows(rows: list[dict[str, object]], due_at: datetime, now: datetime) -> tuple[str, int]:
    """依 sync_log 紀錄與時間決定狀態，回傳 (狀態, 失敗次數)。"""
    if any(r["status"] in DONE_STATUSES for r in rows):
        last = next(r for r in reversed(rows) if r["status"] in DONE_STATUSES)
        return ("missed" if last["status"] == "missed" else "done"), 0
    errors = [r for r in rows if r["status"] == "error"]
    if len(errors) >= MAX_ATTEMPTS:
        return "gaveup", len(errors)
    if now < due_at:
        return "waiting", len(errors)
    if errors:
        finished = _parse(errors[-1]["finished_at"])
        if finished and now - finished < RETRY_BACKOFF:
            return "retry", len(errors)
    return "due", len(errors)


def notion_bound(repo: PostStateRepository, cfg: AppSettings, account_id: str) -> bool:
    from app.notion_sync import NotionSyncService

    return NotionSyncService.for_account(repo, cfg, account_id) is not None


def _notion_done(repo: PostStateRepository, account_id: str, post: CarouselPostPackage) -> bool:
    sync = repo.last_job_by_label(account_id=account_id, job="notion_sync", label=post.post_id)
    updated = repo.post_updated_at(post.post_id)
    if sync is None or not sync.get("finished_at"):
        return False
    return not updated or str(sync["finished_at"]) >= updated


def publish_mode(repo: PostStateRepository, account_id: str, market: MarketProfile) -> str:
    """none｜export（只備發布包）｜publish（交給發布流程）。"""
    binding = repo.get_tiktok_binding(account_id)
    cap = str(binding["publish_capability"]) if binding else "export_only"
    if cap == "export_only":
        return "export"
    return "publish" if market.auto_publish else "none"


def plan(
    repo: PostStateRepository,
    cfg: AppSettings,
    now_utc: datetime,
    account_ids: list[str] | None = None,
) -> list[Action]:
    actions: list[Action] = []
    for acc in repo.list_accounts():
        if account_ids and acc.account_id not in account_ids:
            continue
        market = repo.get_market_profile(acc.account_id)
        if not market.auto_enabled:
            continue
        tz = ZoneInfo(market.timezone)
        now = now_utc.astimezone(tz)
        today = now.date()
        aid, handle = acc.account_id, acc.handle
        posts = {p.post_id: p for p in repo.list_posts(account_id=aid)}

        # 研究
        due = _at(today, market.auto_research_time, tz)
        last = repo.last_job(account_id=aid, job="research", only_ok=True)
        finished = _parse(last["finished_at"]) if last else None
        if finished and now_utc - finished < timedelta(hours=24):
            state, attempts = "done", 0
        else:
            state, attempts = _state_from_rows(repo.job_rows(account_id=aid, job="auto_research", label=today.isoformat()), due, now)
        actions.append(Action(aid, handle, "research", today.isoformat(), due, state, "研究爆款案例", date_str=today.isoformat(), attempts=attempts))

        # 製作今天（補做）與明天的內容
        for d in (today, today + timedelta(days=1)):
            for slot in SLOT_KINDS:
                slot_time = _at(d, market.time_for(slot), tz)
                if d == today and slot_time <= now:
                    continue
                post_id = make_post_id(aid, d.isoformat(), slot)
                post = posts.get(post_id)
                due = _at(d - timedelta(days=1), market.auto_prepare_time, tz)
                if post is not None and post.status not in (PostStatus.FAILED, PostStatus.QUALITY_FAILED):
                    state, attempts = "done", 0
                else:
                    state, attempts = _state_from_rows(repo.job_rows(account_id=aid, job="auto_produce", label=post_id), due, now)
                actions.append(
                    Action(aid, handle, "produce", post_id, due, state, f"製作 {d.isoformat()} {market.time_for(slot)} {market.track_name(slot)}", slot=slot, date_str=d.isoformat(), attempts=attempts)
                )  # fmt: skip

        # 同步 Notion
        bound = notion_bound(repo, cfg, aid)
        for post in posts.values():
            if not bound or post.is_mock or post.status not in NOTION_STATUSES:
                continue
            keep_days = INBOX_WINDOW_DAYS if post.status in (PostStatus.SENT_TO_INBOX, PostStatus.PUBLISHED) else 1
            if post.scheduled_at_taipei[:10] < (today - timedelta(days=keep_days)).isoformat():
                continue
            if _notion_done(repo, aid, post):
                state, attempts = "done", 0
            else:
                rows = repo.job_rows(account_id=aid, job="notion_sync", label=post.post_id)
                last_ok = max((i for i, r in enumerate(rows) if r["status"] == "ok"), default=-1)
                state, attempts = _state_from_rows([r for r in rows[last_ok + 1 :] if r["status"] == "error"], now, now)
            actions.append(Action(aid, handle, "notion", post.post_id, now, state, f"同步 Notion {post.post_id}", slot=post.slot, date_str=post.scheduled_at_taipei[:10], attempts=attempts))

        # 發布／匯出
        mode = publish_mode(repo, aid, market)
        if mode == "none":
            continue
        for post in posts.values():
            d_str = post.scheduled_at_taipei[:10]
            if post.status != PostStatus.READY or post.is_mock or d_str not in {(today + timedelta(days=i)).isoformat() for i in (-1, 0, 1)}:
                continue
            d = date.fromisoformat(d_str)
            due = _at(d, market.time_for(post.slot), tz)
            rows = repo.job_rows(account_id=aid, job="auto_publish", label=post.post_id)
            state, attempts = _state_from_rows(rows, due, now)
            if state == "due" and now > due + PUBLISH_GRACE:
                state = "missed"
            verb = "匯出發布包" if mode == "export" else "發布"
            actions.append(Action(aid, handle, "publish", post.post_id, due, state, f"{verb} {d_str} {market.time_for(post.slot)} {market.track_name(post.slot)}", slot=post.slot, date_str=d_str, attempts=attempts))
    actions.sort(key=lambda a: a.due_at)
    return actions


def next_action_text(actions: list[Action], account_id: str, now_utc: datetime, tz_name: str) -> str:
    """設定頁顯示的「下一個預定動作」。"""
    tz = ZoneInfo(tz_name)
    now = now_utc.astimezone(tz)
    pending = [a for a in actions if a.account_id == account_id and a.state in ("due", "waiting", "retry")]
    if not pending:
        return ""
    a = min(pending, key=lambda x: x.due_at)
    due = a.due_at.astimezone(tz)
    if a.state in ("due", "retry") or due <= now:
        when = "現在" if a.state == "due" else "稍後重試"
    else:
        day = {0: "今天", 1: "明天"}.get((due.date() - now.date()).days, due.strftime("%m/%d"))
        when = f"{day} {due:%H:%M}"
    return f"{when} {a.text}"


def format_plan(actions: list[Action], now_utc: datetime) -> list[str]:
    names = {"due": "到期", "waiting": "等待", "retry": "退避中", "done": "已完成", "gaveup": "已放棄", "missed": "已錯過"}
    lines = []
    for a in actions:
        tz = a.due_at.tzinfo
        lines.append(f"[{names[a.state]}] {a.handle} {a.due_at.astimezone(tz):%m/%d %H:%M} {a.text}" + (f"（失敗 {a.attempts} 次）" if a.attempts else ""))
    return lines


# ---------------------------------------------------------------------------
# 鎖
# ---------------------------------------------------------------------------


class SchedulerBusyError(RuntimeError):
    pass


def acquire_lock(path: Path, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                taken = datetime.fromisoformat(info["at"])
            except (OSError, ValueError, KeyError):
                taken = now - LOCK_STALE - timedelta(seconds=1)
            if now - taken < LOCK_STALE:
                raise SchedulerBusyError("排程器已在另一個程序執行中") from None
            path.unlink(missing_ok=True)  # 殘留的鎖，接管
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "at": now.isoformat()}))
        return
    raise SchedulerBusyError("無法取得排程鎖")


def release_lock(path: Path) -> None:
    path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 通知（同一帳號＋步驟＋目標，狀態沒變就不重複通知）
# ---------------------------------------------------------------------------


def _notify_once(repo: PostStateRepository, cfg: AppSettings, a: Action, state: str, title: str, message: str, now: datetime) -> None:
    key = f"{a.kind}|{a.target}"
    rows = repo.job_rows(account_id=a.account_id, job="auto_notify", label=key)
    if rows and rows[-1]["summary"] == state:
        return
    try:
        send_notification(repo, cfg, title=title, message=message, level="error")
    except Exception:  # noqa: BLE001 通知不能讓排程失敗
        pass
    log_id = repo.log_job_start(account_id=a.account_id, job="auto_notify", label=key, at=now)
    repo.log_job_finish(log_id, status="ok", summary=state, at=now)


def _failure_message(a: Action, error: str, attempts: int) -> tuple[str, str]:
    step = KIND_NAME[a.kind]
    gave_up = attempts >= MAX_ATTEMPTS
    title = f"{a.handle}　{step}{'失敗，已停止重試' if gave_up else '失敗'}"
    lines = [a.text, f"錯誤：{redact_sensitive(error)[:300]}", f"第 {attempts} 次失敗" + ("，不會再自動重試" if gave_up else f"，{int(RETRY_BACKOFF.total_seconds() // 60)} 分鐘後重試")]
    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------


@dataclass
class TickResult:
    done: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    planned: list[Action] = field(default_factory=list)


def _execute(repo: PostStateRepository, cfg: AppSettings, root: Path, a: Action, ex: Executors, now: datetime) -> tuple[bool, str]:
    log_id = repo.log_job_start(account_id=a.account_id, job=a.job, label=a.target, at=now)
    try:
        status, summary = "ok", ""
        if a.kind == "research":
            summary = ex.research(repo, cfg, a.account_id, repo.get_market_profile(a.account_id))
        elif a.kind == "produce":
            assert a.slot is not None
            summary = ex.produce(repo, cfg, root, a.account_id, a.date_str, a.slot)
        elif a.kind == "notion":
            post = next((p for p in repo.list_posts(account_id=a.account_id) if p.post_id == a.target), None)
            if post is None:
                raise RuntimeError("找不到貼文")
            summary = ex.notion(repo, cfg, a.account_id, post)
        else:
            outcome = ex.publish(repo, cfg, a.account_id, a.target)
            if outcome.status == "failed":
                raise RuntimeError(outcome.detail)
            summary = f"{outcome.status}：{outcome.detail}"
            if outcome.status == "blocked":
                status = "skipped"
        repo.log_job_finish(log_id, status=status, summary=redact_sensitive(summary), at=now)
        return True, summary
    except Exception as exc:  # noqa: BLE001
        repo.log_job_finish(log_id, status="error", error=redact_sensitive(str(exc)), at=now)
        return False, str(exc)


def _pending_inbox_draft(repo: PostStateRepository, account_id: str, exclude_post_id: str, now_utc: datetime) -> str:
    """同一帳號還有「已傳到收件匣、尚未標記為已發布」的草稿就回它的 post_id，否則回空字串。"""
    tz = ZoneInfo(repo.get_market_profile(account_id).timezone)
    cutoff = (now_utc.astimezone(tz).date() - timedelta(days=INBOX_WINDOW_DAYS)).isoformat()
    for p in repo.list_posts(account_id=account_id):
        if p.post_id != exclude_post_id and p.status == PostStatus.SENT_TO_INBOX and not p.is_mock and p.scheduled_at_taipei[:10] >= cutoff:
            return p.post_id
    return ""


def _reconcile_inbox(
    repo: PostStateRepository, cfg: AppSettings, ex: Executors, result: TickResult, now_utc: datetime, account_ids: list[str] | None
) -> None:
    """收件匣草稿對帳：使用者在 TikTok 按下發布後，狀態改成 PUBLISHED，接著 Notion 同步會跟著更新。

    查詢失敗不算排程失敗（草稿可能還沒發、網路暫時不通），下一次 tick 再查。
    """
    for acc in repo.list_accounts():
        if account_ids and acc.account_id not in account_ids:
            continue
        cutoff = (now_utc.astimezone(ZoneInfo(repo.get_market_profile(acc.account_id).timezone)).date() - timedelta(days=INBOX_WINDOW_DAYS)).isoformat()
        for post in repo.list_posts(account_id=acc.account_id):
            if post.status != PostStatus.SENT_TO_INBOX or post.is_mock or post.scheduled_at_taipei[:10] < cutoff:
                continue
            try:
                info = ex.reconcile(repo, cfg, acc.account_id, post.post_id)
            except Exception:  # noqa: BLE001
                continue
            if repo.get_post_status(post.post_id) == PostStatus.PUBLISHED:
                result.done.append(f"{acc.handle} {post.post_id}：{info}"[:200])


def tick(
    repo: PostStateRepository,
    cfg: AppSettings,
    project_root: Path,
    *,
    now_utc: datetime | None = None,
    dry_run: bool = False,
    account_id: str | None = None,
    executors: Executors | None = None,
    lock_path: Path | None = None,
) -> TickResult:
    now_utc = now_utc or datetime.now(UTC)
    ids = [account_id] if account_id else None
    result = TickResult(planned=plan(repo, cfg, now_utc, ids))
    if dry_run:
        return result

    lock = lock_path or cfg.sqlite_db_path.expanduser().resolve().parent / "scheduler.lock"
    tick_id = None
    try:
        acquire_lock(lock, now_utc)
    except SchedulerBusyError as exc:
        result.skipped_reason = str(exc)
        return result
    try:
        tick_id = repo.log_job_start(account_id="", job="scheduler_tick", label="排程", at=now_utc)
        for row in repo.running_jobs(since_iso="0"):
            if str(row["job"]).startswith("auto_") and (_parse(row["started_at"]) or now_utc) < now_utc - LOCK_STALE:
                repo.log_job_finish(int(row["id"]), status="error", error="排程中斷，工作沒有完成", at=now_utc)  # type: ignore[arg-type]
        busy = [
            r for r in repo.running_jobs(since_iso=(now_utc - WEB_BUSY_WINDOW).isoformat())
            if not str(r["job"]).startswith("auto_") and r["job"] != "scheduler_tick"
        ]  # fmt: skip
        if busy:
            result.skipped_reason = f"操作台正在執行「{busy[0]['label']}」"
            repo.log_job_finish(tick_id, status="ok", summary=f"略過：{result.skipped_reason}", at=now_utc)
            return result

        ex = executors or default_executors()
        _reconcile_inbox(repo, cfg, ex, result, now_utc, ids)
        for kinds in (("research", "produce"), ("notion", "publish")):
            # 第二輪重新規劃：剛做好的貼文可以馬上同步與匯出，不必等下一次 tick
            for a in (result.planned if kinds[0] == "research" else plan(repo, cfg, now_utc, ids)):
                if a.kind not in kinds:
                    continue
                if a.state == "missed":
                    if any(r["status"] == "missed" for r in repo.job_rows(account_id=a.account_id, job=a.job, label=a.target)):
                        continue
                    log_id = repo.log_job_start(account_id=a.account_id, job=a.job, label=a.target, at=now_utc)
                    repo.log_job_finish(log_id, status="missed", summary="超過發文時間 2 小時，沒有自動發布", at=now_utc)
                    result.failed.append(a.text)
                    _notify_once(repo, cfg, a, "missed", f"{a.handle}　錯過發文時間", f"{a.text}\n超過發文時間 2 小時仍未發布。", now_utc)
                    continue
                if a.state != "due":
                    continue
                if a.kind == "publish" and cfg.tiktok_post_mode == "inbox":
                    waiting = _pending_inbox_draft(repo, a.account_id, a.target, now_utc)
                    if waiting:
                        # TikTok 通知只會打開最舊的待處理草稿；前一篇沒處理完就送下一篇，新草稿會被擋在後面看不到
                        _notify_once(repo, cfg, a, f"inbox_pending|{waiting}", f"{a.handle}　先不送下一篇草稿", f"{a.text}\n收件匣還有一篇沒發布的草稿（{waiting}）。請在 TikTok 發布後，到內容庫按「標記為已發布」，下一篇才會送出。", now_utc)
                        continue
                ok, info = _execute(repo, cfg, project_root, a, ex, now_utc)
                if ok:
                    result.done.append(f"{a.text}：{info}"[:200])
                else:
                    result.failed.append(f"{a.text}：{info}"[:200])
                    attempts = a.attempts + 1
                    title, message = _failure_message(a, info, attempts)
                    _notify_once(repo, cfg, a, "gaveup" if attempts >= MAX_ATTEMPTS else "failed", title, message, now_utc)
        summary = f"完成 {len(result.done)} 項" + (f"，失敗 {len(result.failed)} 項" if result.failed else "")
        repo.log_job_finish(tick_id, status="ok", summary=summary if (result.done or result.failed) else "沒有到期的工作", at=now_utc)
        return result
    except Exception as exc:  # noqa: BLE001
        if tick_id is not None:
            repo.log_job_finish(tick_id, status="error", error=redact_sensitive(str(exc)), at=now_utc)
        raise
    finally:
        release_lock(lock)


def last_tick(repo: PostStateRepository) -> dict[str, object] | None:
    return repo.last_job(account_id="", job="scheduler_tick")


def tick_age_minutes(repo: PostStateRepository, now_utc: datetime | None = None) -> float | None:
    row = last_tick(repo)
    if row is None or not row.get("started_at"):
        return None
    started = _parse(row["started_at"])
    return None if started is None else ((now_utc or datetime.now(UTC)) - started).total_seconds() / 60

