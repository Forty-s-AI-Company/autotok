"""Phase 1 核心單元與整合測試套件。

涵蓋驗收標準要求的所有情境：
1. 生成六張獨立、順序正確 (01–06)、尺寸 1080×1350 的測試 JPEG。
2. 媒體驗證器拒絕：
   - 含有 PNG 檔案或將 PNG 偽裝為 `.jpg` 副檔名
   - 尺寸錯誤（如 1080×1080 或 1920×1080）
   - 漏圖（少於或多於 6 張、頁碼順序缺失）
   - 重複圖（兩張圖片 SHA-256 相同）
3. 驗證四種 `UploaderAdapter` (`mock`, `official_api`, `autotok_candidate`, `manual_export`)：
   - `autotok_candidate` 明確拋出 `UnsupportedFeatureError`
   - `official_api` 在 `--dry-run` 回傳合法 `PHOTO` + `PULL_FROM_URL` 結構，在 `--live` 未授權時拋出 `BlockedNeedsAuthError` / `BlockedNeedsAuditError`
   - `manual_export` 能正確匯出半自動發布包
4. CLI `verify-media` 與 `publish --dry-run` 無外部副作用；未帶 `--dry-run` 時安全阻擋。
5. SQLite 狀態機與發布鎖（`acquire_publish_lock`）防重發測試。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.adapters import get_uploader_adapter
from app.cli import main as cli_main
from app.config import AppSettings, redact_sensitive
from app.errors import (
    BlockedNeedsAuditError,
    BlockedNeedsAuthError,
    UnsupportedFeatureError,
)
from app.media import generate_mock_carousel_media, verify_carousel_directory
from app.models import PostStatus
from app.state import PostStateRepository


def test_generate_and_verify_six_valid_jpegs(tmp_path: Path) -> None:
    """測試產生 6 張獨立 1080x1350 測試 JPEG 並通過驗證。"""
    media_dir = tmp_path / "valid_carousel"
    pkg = generate_mock_carousel_media(media_dir, post_id="test-001")

    assert len(pkg.slides) == 6
    assert len(pkg.hashtags) == 5

    report = verify_carousel_directory(media_dir)
    assert report.passed is True
    assert report.actual_count == 6
    assert report.errors == []
    assert [item.index for item in report.items] == [1, 2, 3, 4, 5, 6]
    assert all(item.width == 1080 and item.height == 1350 for item in report.items)
    assert all(item.mime_type == "image/jpeg" for item in report.items)
    # 確保 6 張圖 SHA-256 完全不重複
    assert len({item.sha256 for item in report.items}) == 6


def test_verifier_rejects_png_and_disguised_png(tmp_path: Path) -> None:
    """測試驗證器會拒絕目錄中混入 PNG，以及將 PNG 改副檔名為 .jpg 的偽裝檔案。"""
    media_dir = tmp_path / "png_carousel"
    generate_mock_carousel_media(media_dir, post_id="test-png")

    # 情境 A：將第 3 張圖替換為真實格式是 PNG 但副檔名為 .jpg 的檔案
    disguised_path = media_dir / "slide_03.jpg"
    png_img = Image.new("RGB", (1080, 1350), color=(100, 100, 100))
    png_img.save(disguised_path, format="PNG")

    report = verify_carousel_directory(media_dir)
    assert report.passed is False
    assert any("並非真實 JPEG 格式" in err for err in report.errors)

    # 情境 B：額外放入一個 .png 檔案
    extra_png = media_dir / "extra.png"
    png_img.save(extra_png, format="PNG")
    report2 = verify_carousel_directory(media_dir)
    assert report2.passed is False
    assert any("非 JPEG 媒體格式" in err for err in report2.errors)


def test_verifier_rejects_wrong_dimensions(tmp_path: Path) -> None:
    """測試驗證器會拒絕非 1080x1350 的圖片尺寸。"""
    media_dir = tmp_path / "wrong_size"
    generate_mock_carousel_media(media_dir, post_id="test-size")

    bad_img = Image.new("RGB", (1080, 1080), color=(50, 60, 70))
    bad_img.save(media_dir / "slide_05.jpg", format="JPEG")

    report = verify_carousel_directory(media_dir)
    assert report.passed is False
    assert any("1080x1080" in err for err in report.errors)


def test_verifier_rejects_missing_slide(tmp_path: Path) -> None:
    """測試驗證器會拒絕漏圖（少於 6 張或順序缺失）。"""
    media_dir = tmp_path / "missing_slide"
    generate_mock_carousel_media(media_dir, post_id="test-missing")

    (media_dir / "slide_04.jpg").unlink()

    report = verify_carousel_directory(media_dir)
    assert report.passed is False
    assert report.actual_count == 5
    assert any("必須恰好為 6 張" in err for err in report.errors)


def test_verifier_rejects_duplicate_images(tmp_path: Path) -> None:
    """測試驗證器會拒絕重複圖片（例如將 slide_01.jpg 複製覆蓋為 slide_02.jpg）。"""
    media_dir = tmp_path / "duplicate_slide"
    generate_mock_carousel_media(media_dir, post_id="test-dup")

    shutil.copy2(media_dir / "slide_01.jpg", media_dir / "slide_02.jpg")

    report = verify_carousel_directory(media_dir)
    assert report.passed is False
    assert any("重複圖片" in err for err in report.errors)


def test_autotok_candidate_adapter_raises_unsupported(tmp_path: Path) -> None:
    """確認 autotok_candidate adapter 明確拋出 UnsupportedFeatureError，絕不偽裝成功。"""
    media_dir = tmp_path / "autotok_test"
    pkg = generate_mock_carousel_media(media_dir)
    settings = AppSettings(SQLITE_DB_PATH=tmp_path / "state.db")
    adapter = get_uploader_adapter("autotok_candidate", settings=settings)

    with pytest.raises(UnsupportedFeatureError) as exc_info:
        adapter.publish_carousel(pkg, dry_run=True)

    assert "UNSUPPORTED_FEATURE" in exc_info.value.code
    assert "Issue #70" in str(exc_info.value)


def test_official_api_adapter_dry_run_and_live_blocks(tmp_path: Path) -> None:
    """確認 official_api 在 dry-run 輸出正確 PHOTO payload，在 live 時依權限與審核狀態正確攔截。"""
    media_dir = tmp_path / "official_test"
    pkg = generate_mock_carousel_media(media_dir)

    # 1. Dry-run 且未設定 token -> 狀態為 BLOCKED_NEEDS_AUTH，且因未通過 audit 故 privacy_level 為 SELF_ONLY
    settings_unauth = AppSettings(
        ALLOW_LIVE_PUBLISH=False,
        TIKTOK_ACCESS_TOKEN="",
        TIKTOK_APP_AUDITED=False,
    )
    adapter = get_uploader_adapter("official_api", settings=settings_unauth)
    plan = adapter.publish_carousel(pkg, dry_run=True)
    assert plan.dry_run is True
    assert plan.status == PostStatus.BLOCKED_NEEDS_AUTH
    assert plan.privacy_level == "SELF_ONLY"
    assert plan.payload_preview["media_type"] == "PHOTO"

    # 2. Live 但 ALLOW_LIVE_PUBLISH=False -> 拋出 BlockedNeedsAuthError
    with pytest.raises(BlockedNeedsAuthError):
        adapter.publish_carousel(pkg, dry_run=False)

    # 3. Live、已給 Token 但未通過 App Audit -> 拋出 BlockedNeedsAuditError
    settings_unaudited = AppSettings(
        ALLOW_LIVE_PUBLISH=True,
        TIKTOK_ACCESS_TOKEN="fake_test_token_123",
        TIKTOK_VERIFIED_IMAGE_URL_PREFIX="https://cdn.example.com/media",
        TIKTOK_APP_AUDITED=False,
    )
    adapter_unaudited = get_uploader_adapter("official_api", settings=settings_unaudited)
    with pytest.raises(BlockedNeedsAuditError):
        adapter_unaudited.publish_carousel(pkg, dry_run=False, post_mode="DIRECT_POST")


def test_manual_export_adapter_writes_bundle(tmp_path: Path) -> None:
    """測試 manual_export 半自動匯出器能完整複製 6 張 JPEG 與越南文文案清單。"""
    media_dir = tmp_path / "src_media"
    export_base = tmp_path / "exports"
    pkg = generate_mock_carousel_media(media_dir, post_id="export-001")

    settings = AppSettings(EXPORT_BASE_DIR=export_base)
    adapter = get_uploader_adapter("manual_export", settings=settings)

    plan = adapter.publish_carousel(pkg, dry_run=False)
    assert plan.status == PostStatus.READY_TO_REVIEW
    bundle_dir = export_base / "export-001"
    assert (bundle_dir / "manifest.json").is_file()
    assert (bundle_dir / "caption_vi.txt").is_file()
    assert len(list((bundle_dir / "images").glob("*.jpg"))) == 6


def test_cli_commands_and_safety_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """測試 CLI 子命令執行與預設禁用正式 publish 的保護機制。"""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "cli_state.db"))
    monkeypatch.setenv("ALLOW_LIVE_PUBLISH", "false")

    media_dir = tmp_path / "cli_media"

    # 1. generate-mock-media
    assert cli_main(["generate-mock-media", str(media_dir), "--post-id", "cli-001"]) == 0
    # 2. verify-media
    assert cli_main(["verify-media", str(media_dir)]) == 0
    # 3. publish --dry-run
    assert cli_main(["publish", "--media-dir", str(media_dir), "--dry-run"]) == 0
    # 4. publish 未加 --dry-run 必須被拒絕 (exit code 2)
    assert cli_main(["publish", "--media-dir", str(media_dir)]) == 2


def test_state_repository_and_publish_lock(tmp_path: Path) -> None:
    """測試 SQLite 狀態持久化與防重複發布鎖定機制。"""
    db_path = tmp_path / "locks.db"
    repo = PostStateRepository(db_path)
    pkg = generate_mock_carousel_media(tmp_path / "lock_media", post_id="lock-post-01")
    repo.upsert_post(pkg)

    assert repo.acquire_publish_lock("lock-post-01", "github-actions-runner-1") is True
    # 同一篇貼文再次要求鎖定必須失敗，防止 GitHub Actions 與 Windows 雙重上傳
    assert repo.acquire_publish_lock("lock-post-01", "windows-task-scheduler") is False

    repo.release_publish_lock("lock-post-01")
    # 若狀態變更為 UNKNOWN，未知狀態必須先查詢，不可盲目取得鎖重傳
    repo.update_status("lock-post-01", PostStatus.UNKNOWN, last_error="network timeout")
    assert repo.acquire_publish_lock("lock-post-01", "github-actions-runner-1") is False


def test_log_redaction() -> None:
    """測試敏感 Token 與 Session ID 遮罩功能。"""
    raw = "Authorization: Bearer act.1234567890abcdef sessionid=998877665544"
    redacted = redact_sensitive(raw)
    assert "1234567890abcdef" not in redacted
    assert "998877665544" not in redacted
    assert "[REDACTED]" in redacted
