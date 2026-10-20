"""Phase 3｜越南文六頁製圖、分離式文字排版、視覺多樣性與 Quality Gate 限次重試測試套件。

涵蓋 MASTER_SPEC.md 第 3, 4, 6, 11 節要求：
1. 每日三時段（07:00 家庭情緒 / 11:30 台灣生活 / 18:00 圖文行銷）完整產出：
   - 6 張獨立 1080×1350 越南文 JPEG (`vi/slide_01.jpg` .. `slide_06.jpg`)
   - 6 張獨立 1080×1350 繁體中文對照 JPEG (`zh_tw/slide_01.jpg` .. `slide_06.jpg`)
   - 恰好 5 個 Hashtag、越中逐頁翻譯對照
2. 視覺多樣性檢核：拒絕 6 張全用同一純色平面背景的偷工減料圖片。
3. 合規詞彙審核：攔截「保證高薪」、「保證過簽 (`bao đậu visa`)」等違規宣稱。
4. 問題頁限次重製（Retry）與 `QUALITY_FAILED` 阻擋：
   - 當某單頁（如第 3 頁）初次渲染尺寸損壞時，Quality Gate 能自動定位並重製該問題頁直到通過。
   - 若超過 `max_retries` 仍不合格，則拋出 `MediaValidationError` (`QUALITY_FAILED`)。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from app.cli import main as cli_main
from app.errors import MediaValidationError
from app.models import ContentSlot, PostStatus
from app.production import (
    SceneImageProvider,
    audit_editorial_compliance,
    produce_carousel_with_qa_retry,
    verify_visual_diversity_and_text_integrity,
)


def test_produce_three_daily_slots_with_vi_and_zh_jpegs(tmp_path: Path) -> None:
    """測試三時段（07:00 / 11:30 / 18:00）皆能產出合規且構圖多樣的 6 張越文圖與 6 張繁中對照圖。"""
    for slot in [ContentSlot.MORNING_0700, ContentSlot.NOON_1130, ContentSlot.EVENING_1800]:
        pkg, report, zh_dir = produce_carousel_with_qa_retry(
            slot=slot,
            post_id=f"test-{slot.name}",
            scheduled_date_str="2026-10-21",
            output_base_dir=tmp_path,
        )
        assert pkg.status == PostStatus.READY
        assert report.passed is True
        assert report.actual_count == 6
        assert len(pkg.hashtags) == 5
        assert len(list(zh_dir.glob("slide_0*.jpg"))) == 6


def test_qa_gate_auto_retries_corrupted_single_slide(tmp_path: Path) -> None:
    """測試當初次生成時第 4 頁尺寸錯誤，Quality Gate 會自動重製第 4 頁並最終通過驗證。"""
    pkg, report, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="retry-success-001",
        scheduled_date_str="2026-10-21",
        output_base_dir=tmp_path,
        max_retries=2,
        simulate_first_attempt_corrupt_slide=4,
    )
    assert pkg.status == PostStatus.READY
    assert report.passed is True
    assert all(item.width == 1080 and item.height == 1350 for item in report.items)


class _BrokenFlatSceneProvider(SceneImageProvider):
    """故意每頁都回傳完全相同純色背景的不合格 Provider，測試視覺多樣性 Gate 攔截。"""

    provider_name = "broken_flat_mock"

    def render_background(
        self,
        *,
        slot: ContentSlot,
        slide_index: int,
        attempt: int = 1,
    ) -> Image.Image:
        return Image.new("RGB", (1080, 1350), color=(50, 50, 50))


def test_qa_gate_rejects_flat_identical_backgrounds_after_max_retries(tmp_path: Path) -> None:
    """測試若背景無視覺多樣性（全組同一純色模板），重試耗盡後會標記 QUALITY_FAILED 並拋出例外。"""
    with pytest.raises(MediaValidationError) as exc_info:
        produce_carousel_with_qa_retry(
            slot=ContentSlot.MORNING_0700,
            post_id="flat-fail-001",
            scheduled_date_str="2026-10-21",
            output_base_dir=tmp_path,
            scene_provider=_BrokenFlatSceneProvider(),
            max_retries=1,
        )
    assert exc_info.value.code == "QUALITY_FAILED"
    assert any("視覺多樣性不足" in r for r in exc_info.value.reasons)


def test_editorial_compliance_blocks_forbidden_claims(tmp_path: Path) -> None:
    """測試文案合規審核會攔截『保證高薪』、『bao đậu visa』等違規承諾。"""
    violations = audit_editorial_compliance(
        ["Tuyển dụng việc nhẹ lương cao, bao đậu visa Đài Loan 100%"]
    )
    assert len(violations) >= 2

    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="comp-test",
        scheduled_date_str="2026-10-21",
        output_base_dir=tmp_path,
    )
    pkg.vi_caption = "Cam kết lương cao khi sang Đài Loan"
    passed, errs = verify_visual_diversity_and_text_integrity(pkg, pkg.media_dir)
    assert passed is False
    assert any("cam kết lương cao" in e for e in errs)


def test_produce_daily_cli_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """測試 CLI `produce-daily` 命令。"""
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "p3_cli.db"))
    out_dir = tmp_path / "daily_out"
    rc = cli_main(["produce-daily", "--date", "2026-10-22", "--slot", "11:30", "--output-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "2026-10-22" / "post-2026-10-22-1130" / "vi" / "slide_01.jpg").is_file()
    assert (out_dir / "2026-10-22" / "post-2026-10-22-1130" / "zh_tw" / "slide_06.jpg").is_file()
