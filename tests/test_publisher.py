"""發布流程：閘門、成功路徑、不明結果不重傳、dry-run、圖片託管。全程不碰真實 TikTok。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from app import credentials
from app.config import AppSettings
from app.integrations.tiktok import TikTokTokens
from app.models import ContentSlot, PostStatus
from app.production import produce_carousel_with_qa_retry
from app.publisher import SyncDirImageHost, build_payload, publish_post
from app.state import PostStateRepository

ACC = "carry-vietnam-tw"
PID = "post-2026-10-21-1130"


class Recorder:
    def __init__(self, *, statuses: list[dict] | None = None, init_status: int = 200, creator_options=("SELF_ONLY", "PUBLIC_TO_EVERYONE")) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.statuses = statuses if statuses is not None else [{"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["777"]}]
        self.init_status = init_status
        self.options = list(creator_options)
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content and req.headers.get("content-type", "").startswith("application/json") else dict(httpx.QueryParams(req.content.decode()))
        self.calls.append((req.url.path, body))
        if req.url.path.endswith("/oauth/token/"):
            return httpx.Response(200, json={"access_token": "new_act", "refresh_token": "new_rft", "expires_in": 86400})
        if req.url.path.endswith("/creator_info/query/"):
            return httpx.Response(200, json={"data": {"creator_username": "carry.vietnam.tw", "privacy_level_options": self.options}, "error": {"code": "ok"}})
        if req.url.path.endswith("/content/init/"):
            if self.init_status != 200:
                return httpx.Response(self.init_status, json={"error": {"code": "spam_risk_too_many_posts", "message": "cap"}})
            return httpx.Response(200, json={"data": {"publish_id": "pub1"}, "error": {"code": "ok"}})
        if req.url.path.endswith("/status/fetch/"):
            st = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={"data": st, "error": {"code": "ok"}})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [p.rsplit("/", 2)[-2] for p, _ in self.calls]


@pytest.fixture()
def env(tmp_path: Path):
    repo = PostStateRepository(tmp_path / "t.db")
    pkg, _, _ = produce_carousel_with_qa_retry(slot=ContentSlot.NOON_1130, post_id=PID, scheduled_date_str="2026-10-21", output_base_dir=tmp_path / "out")
    pkg.is_mock = False
    pkg.status = PostStatus.READY
    repo.upsert_post(pkg)
    sync_dir = tmp_path / "site"
    cfg = AppSettings(
        SQLITE_DB_PATH=tmp_path / "t.db", ALLOW_LIVE_PUBLISH=True, TIKTOK_APP_AUDITED=True, TIKTOK_CLIENT_KEY="k", TIKTOK_CLIENT_SECRET="s",
        TIKTOK_VERIFIED_IMAGE_URL_PREFIX="https://cdn.example.com/m", IMAGE_HOST_SYNC_DIR=str(sync_dir), EXPORT_BASE_DIR=tmp_path / "exp",
    )
    tokens = TikTokTokens("act", "rft", time.time() + 3600, "o1")
    credentials.set_secret(f"tiktok:{ACC}", tokens.dumps())
    repo.save_tiktok_binding(ACC, token_ref=f"tiktok:{ACC}", publish_capability="public")
    return repo, cfg, sync_dir


def run(env, rec: Recorder | None = None, **kw):
    repo, cfg, _ = env
    rec = rec or Recorder()
    out = publish_post(repo, cfg, ACC, PID, transport=rec.transport, sleep=lambda s: None, **kw)
    return out, rec


def test_export_when_no_authorisation(env) -> None:
    repo, cfg, _ = env
    repo.save_tiktok_binding(ACC, token_ref="", publish_capability="export_only")
    out = publish_post(repo, cfg, ACC, PID)
    assert out.status == "exported" and (Path(out.export_dir) / "manifest.json").is_file()


@pytest.mark.parametrize("mutate,needle", [
    (lambda repo, cfg: setattr(cfg, "allow_live_publish", False), "ALLOW_LIVE_PUBLISH"),
    (lambda repo, cfg: setattr(cfg, "image_host_sync_dir", ""), "圖片託管"),
    (lambda repo, cfg: repo.update_status(PID, PostStatus.UNKNOWN), "UNKNOWN"),
    (lambda repo, cfg: repo.update_status(PID, PostStatus.PUBLISHED), "不重複"),
])
def test_gates_block_without_network(env, mutate, needle) -> None:
    repo, cfg, _ = env
    mutate(repo, cfg)
    out, rec = run(env)
    assert out.status == "blocked" and needle in out.detail
    assert rec.calls == []


def test_mock_post_and_failed_quality_are_blocked(env, tmp_path: Path) -> None:
    repo, cfg, _ = env
    pkg = repo.list_posts(account_id=ACC)[0]
    pkg.is_mock = True
    repo.upsert_post(pkg)
    out, rec = run(env)
    assert out.status == "blocked" and "模擬" in out.detail and rec.calls == []
    pkg.is_mock = False
    repo.upsert_post(pkg)
    next(pkg.media_dir.glob("slide_01.jpg")).unlink()
    out, rec = run(env)
    assert out.status == "blocked" and "品質檢查" in out.detail and rec.calls == []


def test_lock_held_by_another_runner_blocks(env) -> None:
    repo, _, _ = env
    assert repo.acquire_publish_lock(PID, "someone-else")
    out, rec = run(env)
    assert out.status == "blocked" and "鎖" in out.detail
    assert "content" not in rec.paths()


def test_success_public_flow(env) -> None:
    repo, cfg, sync_dir = env
    out, rec = run(env, Recorder(statuses=[{"status": "PROCESSING_DOWNLOAD"}, {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["777"]}]))
    assert out.status == "published" and out.platform_post_id == "777"
    init_body = next(b for p, b in rec.calls if p.endswith("/content/init/"))
    assert init_body["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE" and init_body["source_info"]["photo_images"][0].startswith("https://cdn.example.com/m/")
    assert repo.get_post_status(PID) == PostStatus.PUBLISHED
    assert not (sync_dir / PID).exists()  # 成功後清理託管檔案
    assert repo.acquire_publish_lock(PID, "x") is False  # 已發布，不可再發


def test_unaudited_app_is_always_self_only(env) -> None:
    repo, cfg, _ = env
    cfg.tiktok_app_audited = False
    out, rec = run(env)
    assert out.status == "private"
    assert next(b for p, b in rec.calls if p.endswith("/content/init/"))["post_info"]["privacy_level"] == "SELF_ONLY"


def test_self_only_capability_never_public(env) -> None:
    repo, cfg, _ = env
    repo.save_tiktok_binding(ACC, token_ref=f"tiktok:{ACC}", publish_capability="self_only")
    out, rec = run(env)
    assert out.status == "private"


def test_privacy_option_mismatch_blocks_before_init(env) -> None:
    out, rec = run(env, Recorder(creator_options=("SELF_ONLY",)))
    assert out.status == "blocked" and "PUBLIC_TO_EVERYONE" in out.detail
    assert not any(p.endswith("/content/init/") for p, _ in rec.calls)


def test_poll_timeout_marks_unknown_and_never_resends(env) -> None:
    repo, cfg, _ = env
    out, rec = run(env, Recorder(statuses=[{"status": "PROCESSING_DOWNLOAD"}]))
    assert out.status == "failed" and "UNKNOWN" in out.detail
    assert repo.get_post_status(PID) == PostStatus.UNKNOWN
    inits = sum(1 for p, _ in rec.calls if p.endswith("/content/init/"))
    assert inits == 1
    out2, rec2 = run(env)
    assert out2.status == "blocked" and not rec2.calls


def test_tiktok_rejection_marks_failed_and_releases_lock(env) -> None:
    repo, cfg, sync_dir = env
    out, rec = run(env, Recorder(init_status=403))
    assert out.status == "failed"
    assert repo.get_post_status(PID) == PostStatus.FAILED
    assert repo.acquire_publish_lock(PID, "retry-later") is True  # 鎖已釋放，可人工重試
    assert not (sync_dir / PID).exists()


def test_processing_failure_reason_is_reported(env) -> None:
    out, _ = run(env, Recorder(statuses=[{"status": "FAILED", "fail_reason": "photo_pull_failed"}]))
    assert out.status == "failed" and "photo_pull_failed" in out.detail


def test_expiring_token_is_refreshed_and_saved(env) -> None:
    repo, cfg, _ = env
    credentials.set_secret(f"tiktok:{ACC}", TikTokTokens("old", "old_r", time.time() + 10, "o").dumps())
    out, rec = run(env)
    assert out.status == "published"
    assert any(p.endswith("/oauth/token/") for p, _ in rec.calls)
    saved = TikTokTokens.loads(credentials.get_secret(f"tiktok:{ACC}"))
    assert saved and saved.access_token == "new_act" and saved.refresh_token == "new_rft"


def test_dry_run_changes_nothing_and_calls_nothing(env) -> None:
    repo, cfg, sync_dir = env
    out, rec = run(env, dry_run=True)
    assert out.status == "exported" and "dry-run" in out.detail and "PUBLIC_TO_EVERYONE" in out.detail
    assert rec.calls == [] and not sync_dir.exists()
    assert repo.get_post_status(PID) == PostStatus.READY


def test_image_host_copy_and_cleanup(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"x")
    host = SyncDirImageHost(tmp_path / "site", "https://cdn.example.com/m/")
    urls = host.upload("p1", [src])
    assert urls == ["https://cdn.example.com/m/p1/a.jpg"] and (tmp_path / "site/p1/a.jpg").is_file()
    host.cleanup("p1")
    assert not (tmp_path / "site/p1").exists()


def test_payload_limits(env) -> None:
    repo, _, _ = env
    pkg = repo.list_posts(account_id=ACC)[0]
    pkg.vi_caption = "x" * 5000
    payload = build_payload(pkg, ["https://a/b.jpg"], "SELF_ONLY")
    assert len(payload["post_info"]["description"]) <= 4000 and len(payload["post_info"]["title"]) <= 90  # type: ignore[index]
    assert payload["post_info"]["is_aigc"] is True  # type: ignore[index]


def test_network_drop_on_init_is_unknown_not_failed(env) -> None:
    repo, cfg, _ = env
    rec = Recorder()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/content/init/"):
            raise httpx.ConnectError("boom")
        return rec.handle(req)

    out = publish_post(repo, cfg, ACC, PID, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    assert out.status == "failed" and "UNKNOWN" in out.detail
    assert repo.get_post_status(PID) == PostStatus.UNKNOWN


# ---------- 收件匣模式（MEDIA_UPLOAD）：傳到草稿，由使用者在 TikTok App 內發布 ----------


def test_inbox_mode_sends_draft_without_privacy_and_never_claims_published(env) -> None:
    repo, cfg, sync_dir = env
    cfg.tiktok_post_mode = "inbox"
    rec = Recorder(statuses=[{"status": "PROCESSING_DOWNLOAD"}, {"status": "SEND_TO_USER_INBOX"}])
    out, rec = run(env, rec)
    assert out.status == "inbox" and "收件匣" in out.detail and "AI 生成內容" in out.detail
    init_body = next(b for p, b in rec.calls if p.endswith("/content/init/"))
    assert init_body["post_mode"] == "MEDIA_UPLOAD"
    assert set(init_body["post_info"]) == {"title", "description"}  # 隱私、留言、音樂、AI 標示都由使用者在 App 內選
    assert init_body["source_info"]["photo_images"][0].startswith("https://cdn.example.com/m/")
    assert "creator_info" not in rec.paths()  # 收件匣模式不需要檢查隱私選項
    assert repo.get_post_status(PID) == PostStatus.SENT_TO_INBOX  # 不是 PUBLISHED：還沒公開
    assert not (sync_dir / PID).exists()
    assert repo.acquire_publish_lock(PID, "x") is False  # 已送出，不可重複送


def test_inbox_mode_blocks_resend_and_dry_run_reports_plan(env) -> None:
    repo, cfg, _ = env
    cfg.tiktok_post_mode = "inbox"
    out, rec = run(env, dry_run=True)
    assert out.status == "exported" and "收件匣" in out.detail and rec.calls == []
    repo.update_status(PID, PostStatus.SENT_TO_INBOX)
    out, rec = run(env)
    assert out.status == "blocked" and rec.calls == []


def test_inbox_mode_failure_is_reported(env) -> None:
    repo, cfg, _ = env
    cfg.tiktok_post_mode = "inbox"
    out, _ = run(env, Recorder(statuses=[{"status": "FAILED", "fail_reason": "photo_pull_failed"}]))
    assert out.status == "failed" and "photo_pull_failed" in out.detail
    assert repo.get_post_status(PID) == PostStatus.FAILED


# ---------- 收件匣草稿對帳 ----------


def _sent_to_inbox(env) -> None:
    repo, _, _ = env
    repo.update_status(PID, PostStatus.SENT_TO_INBOX, platform_post_id="pub1")


def test_reconcile_marks_published_once_user_posts_in_app(env) -> None:
    from app.publisher import reconcile_inbox_post

    repo, cfg, _ = env
    _sent_to_inbox(env)
    rec = Recorder(statuses=[{"status": "SEND_TO_USER_INBOX"}, {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["777"]}])
    assert "還沒發布" in reconcile_inbox_post(repo, cfg, ACC, PID, transport=rec.transport)
    assert repo.get_post_status(PID) == PostStatus.SENT_TO_INBOX
    assert "PUBLISHED" in reconcile_inbox_post(repo, cfg, ACC, PID, transport=rec.transport)
    assert repo.get_post_status(PID) == PostStatus.PUBLISHED and repo.get_platform_post_id(PID) == "777"
    assert set(rec.paths()) == {"fetch"}  # 只查詢狀態，沒有重新上傳


def test_reconcile_ignores_posts_that_are_not_inbox_drafts_and_needs_publish_id(env) -> None:
    from app.publisher import reconcile_inbox_post

    repo, cfg, _ = env
    rec = Recorder()
    assert "略過" in reconcile_inbox_post(repo, cfg, ACC, PID, transport=rec.transport)  # READY
    repo.update_status(PID, PostStatus.SENT_TO_INBOX)  # 沒有 publish_id
    assert "publish_id" in reconcile_inbox_post(repo, cfg, ACC, PID, transport=rec.transport)
    assert rec.calls == [] and repo.get_post_status(PID) == PostStatus.SENT_TO_INBOX
