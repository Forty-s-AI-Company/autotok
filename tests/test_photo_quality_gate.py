"""Codex 生圖的品質檢查：擋簡陋單調的圖、擋重複構圖；不合格的頁一律請 Codex 重畫，不可用程式繪製的版面頂替。

背景：舊版用「平均顏色差 < 2.5」判定相似，兩張完全不同的暖色調照片也會被誤判，
被判定的頁就被程式繪製的純色底＋文字方框頂替，發出去的貼文出現幾張很醜的圖。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageStat

from app.models import ContentSlot, PostStatus
from app.production import (
    produce_carousel_with_qa_retry,
    resize_and_crop_to_1080x1350,
    verify_photo_quality,
)


def photo(seed: int) -> Image.Image:
    """像照片的測試圖：隨機色塊放大，色彩多、細節多；不同 seed 構圖完全不同。"""
    rng = random.Random(seed)
    small = Image.new("RGB", (54, 68))
    small.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256)) for _ in range(54 * 68)])
    return small.resize((1080, 1350), Image.Resampling.BICUBIC)


def plain_card() -> Image.Image:
    """這次醜圖的樣子：漸層底加一個深色文字方框。"""
    im = Image.new("RGB", (1080, 1350))
    d = ImageDraw.Draw(im)
    for y in range(1350):
        d.line([(0, y), (1080, y)], fill=(150 - y // 20, 90 - y // 40, 30))
    d.rounded_rectangle((100, 300, 980, 1000), radius=30, fill=(40, 30, 30))
    return im


def save_set(folder: Path, images: list[Image.Image]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, im in enumerate(images, start=1):
        im.save(folder / f"slide_{i:02d}.jpg", quality=92)


def test_distinct_photos_pass_even_when_average_colors_are_close(tmp_path: Path) -> None:
    imgs = [photo(s) for s in (1, 2, 3, 4, 5)]
    save_set(tmp_path, imgs)
    # 舊規則會誤判：隨機照片的平均顏色幾乎一樣
    means = [ImageStat.Stat(Image.open(tmp_path / f"slide_{i:02d}.jpg")).mean for i in range(1, 6)]
    assert min(sum((a - b) ** 2 for a, b in zip(means[i], means[j], strict=True)) ** 0.5 for i in range(5) for j in range(i + 1, 5)) < 20
    assert verify_photo_quality(tmp_path, 5) == []


def test_plain_text_card_is_rejected(tmp_path: Path) -> None:
    save_set(tmp_path, [photo(1), plain_card(), photo(3)])
    errors = verify_photo_quality(tmp_path, 3)
    assert any("slide_02.jpg" in e and "過於單調" in e for e in errors)
    assert not any("slide_01.jpg" in e or "slide_03.jpg" in e for e in errors if "過於單調" in e)


def test_duplicate_composition_is_rejected(tmp_path: Path) -> None:
    save_set(tmp_path, [photo(1), photo(2), photo(2), photo(4)])
    errors = verify_photo_quality(tmp_path, 4)
    assert any("slide_02.jpg" in e and "slide_03.jpg" in e and "過度相似" in e for e in errors)


class FakeCodex:
    """假的 Codex 生圖器：只補缺的 PNG；第 1 次讓第 3 頁與第 2 頁構圖重複，重畫時給不同的圖。"""

    SLIDE_SCENE_BRIEFS = {s: ["scene"] for s in ContentSlot}

    def __init__(self) -> None:
        self.calls: list[tuple[list[int], list[str]]] = []

    def generate_slot_via_codex(self, *, slot, vi_out_dir, zh_out_dir, workspace_dir, raw_slides_override, scene_briefs_override=None, **kw):  # noqa: ANN001, ANN003
        raw = vi_out_dir.parent / "raw_codex_png"
        raw.mkdir(parents=True, exist_ok=True)
        vi_out_dir.mkdir(parents=True, exist_ok=True)
        zh_out_dir.mkdir(parents=True, exist_ok=True)
        first = not self.calls
        missing = [i for i in range(1, len(raw_slides_override) + 1) if not (raw / f"vi_{i:02d}.png").is_file()]
        self.calls.append((missing, list(scene_briefs_override or self.SLIDE_SCENE_BRIEFS[slot])))
        for i in missing:
            seed = (100 + (2 if (first and i == 3) else i)) if first else 9000 + i
            for lang in ("vi", "zh"):
                photo(seed).save(raw / f"{lang}_{i:02d}.png")
        for i in range(1, len(raw_slides_override) + 1):
            resize_and_crop_to_1080x1350(raw / f"vi_{i:02d}.png", vi_out_dir / f"slide_{i:02d}.jpg")
            resize_and_crop_to_1080x1350(raw / f"zh_{i:02d}.png", zh_out_dir / f"slide_{i:02d}.jpg")


class NeverProcedural:
    """程式繪製的備援版面；Codex 模式下不可被呼叫。"""

    provider_name = "never"

    def render_background(self, **kw):  # noqa: ANN003
        raise AssertionError("Codex 模式不可用程式繪製的版面頂替問題頁")


def test_bad_codex_slides_are_redrawn_by_codex_not_replaced_by_procedural_art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    fake = FakeCodex()
    pkg, report, _ = produce_carousel_with_qa_retry(
        slot=ContentSlot.NOON_1130,
        post_id="post-x",
        scheduled_date_str="2026-10-26",
        output_base_dir=tmp_path / "out",
        use_codex_cli=True,
        codex_generator=fake,  # type: ignore[arg-type]
        scene_provider=NeverProcedural(),  # type: ignore[arg-type]
    )
    assert pkg.status == PostStatus.READY and report.passed
    assert len(fake.calls) == 2
    retry_missing, retry_briefs = fake.calls[1]
    assert retry_missing == [2, 3]  # 重複的兩頁都被刪掉重畫，其他頁沒動
    assert "Compose this slide distinctly" in retry_briefs[1] and "Compose this slide distinctly" in retry_briefs[2]
    assert "Compose this slide distinctly" not in retry_briefs[0]
    # 最終圖片就是 Codex（假的）重畫的成品，不是別的東西
    for i in (2, 3):
        resize_and_crop_to_1080x1350(tmp_path / f"out/post-x/raw_codex_png/vi_{i:02d}.png", tmp_path / f"expected_{i}.jpg")
        assert (tmp_path / f"expected_{i}.jpg").read_bytes() == (tmp_path / f"out/post-x/vi/slide_{i:02d}.jpg").read_bytes()
    assert pkg.slides[1].sha256 != pkg.slides[2].sha256


def test_codex_prompt_has_no_dots_and_requires_rich_photography() -> None:
    import inspect

    from app.production import ChatGPTCodexCliCarouselGenerator

    src = inspect.getsource(ChatGPTCodexCliCarouselGenerator.generate_slot_via_codex)
    assert "progress dots" not in src
    assert "NO dots, circles" in src and "top-right corner" in src
    assert "NEVER output a plain, solid-color or gradient background" in src
