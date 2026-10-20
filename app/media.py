"""六張獨立 JPEG 照片輪播之產生器與品質驗證器（Quality Gate）。

遵循 MASTER_SPEC.md 第 3、6 節與 PHASE1_ANTIGRAVITY_PROMPT.md 第 6 點：
- 產生明顯標記 TEST / MOCK 的 6 張獨立 1080×1350 (4:5) JPEG 圖片，順序 01–06。
- 驗證器嚴格檢查：
  1. 目錄存在且恰好 6 張圖片
  2. 檔名順序符合 01..06（如 slide_01.jpg .. slide_06.jpg）
  3. 檔案真實 MIME / 標頭必須為 JPEG（拒絕 PNG 或偽裝副檔名）
  4. 尺寸必須精確為 1080×1350 (4:5)
  5. SHA-256 檢查：6 張圖片不得有重複圖（防止同一張圖複製湊數）
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.errors import MediaValidationError
from app.models import (
    CarouselPostPackage,
    ContentSlot,
    MediaValidationItem,
    MediaValidationReport,
    PostStatus,
    SlideItem,
)

EXPECTED_WIDTH = 1080
EXPECTED_HEIGHT = 1350
EXPECTED_SLIDE_COUNT = 6
JPEG_MAGIC_PREFIX = b"\xff\xd8\xff"
SLIDE_FILENAME_PATTERN = re.compile(r"^(?:slide_|page_|img_)?0?([1-6])\.(?:jpg|jpeg)$", re.IGNORECASE)

# 預設測試用六頁台越主題內容（明顯標示 TEST / MOCK）
DEFAULT_MOCK_SLIDES: list[tuple[str, str]] = [
    (
        "[TEST/MOCK 01] 6 điều người Việt mới sang Đài Loan cần biết",
        "【測試樣本 01】剛到台灣的越南朋友必知的 6 件事",
    ),
    (
        "[TEST/MOCK 02] Làm quen với thẻ EasyCard (Du Du Khả) khi đi tàu điện và xe buýt",
        "【測試樣本 02】熟悉悠遊卡：搭捷運與公車最方便的工具",
    ),
    (
        "[TEST/MOCK 03] Phân loại rác và nghe tiếng nhạc xe rác buổi tối tại Đài Loan",
        "【測試樣本 03】認識台灣垃圾分類與傍晚垃圾車音樂文化",
    ),
    (
        "[TEST/MOCK 04] Cách giữ liên lạc với gia đình và cộng đồng người Việt hợp pháp",
        "【測試樣本 04】如何與家人保持聯繫並參與合法在台越南社群",
    ),
    (
        "[TEST/MOCK 05] Ẩm thực chợ đêm Đài Loan: những món dễ ăn và hợp khẩu vị",
        "【測試樣本 05】台灣夜市美食指南：合胃口又平價的道地小吃",
    ),
    (
        "[TEST/MOCK 06] Lưu lại bài viết & theo dõi @carry.vietnam.tw để xem thêm kinh nghiệm!",
        "【測試樣本 06】收藏本篇並追蹤 @carry.vietnam.tw 獲取更多實用生活經驗！",
    ),
]

# 每頁使用不同背景色調與幾何裝飾區分構圖，確保視覺與 SHA-256 皆不重複
SLIDE_PALETTES: list[tuple[tuple[int, int, int], tuple[int, int, int]]] = [
    ((24, 58, 74), (235, 176, 53)),
    ((46, 74, 49), (242, 208, 107)),
    ((74, 44, 42), (240, 168, 104)),
    ((38, 50, 84), (138, 196, 208)),
    ((68, 42, 74), (232, 165, 196)),
    ((28, 68, 64), (245, 210, 89)),
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """嘗試載入系統支援越南文與繁體中文的字型，若無則退回預設字型。"""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msjh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def generate_mock_carousel_media(output_dir: Path, post_id: str = "mock-post-001") -> CarouselPostPackage:
    """在指定目錄生成 6 張可重現、標記 TEST/MOCK 的 1080×1350 獨立 JPEG 測試圖，並回傳完整資料包。"""
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    title_font = _load_font(42)
    body_font = _load_font(34)
    watermark_font = _load_font(28)

    slides: list[SlideItem] = []

    for idx, (vi_text, zh_text) in enumerate(DEFAULT_MOCK_SLIDES, start=1):
        bg_color, accent_color = SLIDE_PALETTES[idx - 1]
        img = Image.new("RGB", (EXPECTED_WIDTH, EXPECTED_HEIGHT), color=bg_color)
        draw = ImageDraw.Draw(img)

        # 繪製安全區邊框與頂部/底部識別橫幅（每頁位置與幾何構圖略作變化）
        margin = 70
        draw.rectangle(
            [margin, margin, EXPECTED_WIDTH - margin, EXPECTED_HEIGHT - margin],
            outline=accent_color,
            width=5,
        )

        # 繪製頁碼專屬視覺區塊，確保 6 張圖構圖多樣且非重複
        bar_top = 180 + (idx * 35)
        draw.rectangle(
            [margin + 40, bar_top, EXPECTED_WIDTH - margin - 40, bar_top + 140],
            fill=accent_color,
        )
        draw.text(
            (margin + 70, bar_top + 40),
            f"TEST / MOCK ONLY - SLIDE {idx:02d} / 06",
            fill=bg_color,
            font=title_font,
        )

        # 繪製品牌與測試警告標記
        draw.text(
            (margin + 40, margin + 30),
            "Carry | Đài Loan × Việt Nam (@carry.vietnam.tw)",
            fill=(255, 255, 255),
            font=watermark_font,
        )

        # 繪製越南文與繁體中文對照文字
        text_y = bar_top + 220
        draw.text(
            (margin + 50, text_y),
            f"VI: {vi_text[:48]}",
            fill=(255, 255, 255),
            font=body_font,
        )
        if len(vi_text) > 48:
            draw.text(
                (margin + 50, text_y + 55),
                f"    {vi_text[48:]}",
                fill=(255, 255, 255),
                font=body_font,
            )

        draw.text(
            (margin + 50, text_y + 150),
            f"ZH: {zh_text}",
            fill=(220, 235, 245),
            font=body_font,
        )

        # 底部頁碼與尺寸標記
        draw.text(
            (margin + 40, EXPECTED_HEIGHT - margin - 70),
            f"Phase 1 Feasibility Mock | 1080x1350 JPEG | Page {idx:02d}/06 | Post: {post_id}",
            fill=accent_color,
            font=watermark_font,
        )

        file_path = output_dir / f"slide_{idx:02d}.jpg"
        img.save(file_path, format="JPEG", quality=92, optimize=True)

        sha256_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
        slides.append(
            SlideItem(
                index=idx,
                vi_text=vi_text,
                zh_tw_translation=zh_text,
                image_path=file_path,
                sha256=sha256_hash,
            )
        )

    return CarouselPostPackage(
        post_id=post_id,
        slot=ContentSlot.NOON_1130,
        scheduled_at_taipei="2026-10-21T11:30:00+08:00",
        vi_title="[MOCK] 6 điều người Việt mới sang Đài Loan cần biết",
        zh_tw_title="【測試樣本】剛到台灣的越南朋友必知的 6 件事",
        vi_caption=(
            "[TEST/MOCK] Tổng hợp 6 kinh nghiệm sống tại Đài Loan dành cho người Việt mới sang. "
            "Lưu lại để dùng ngay khi cần nhé!"
        ),
        zh_tw_caption="【測試樣本】為剛到台灣的越南朋友整理 6 個台灣生活實用經驗，先收藏起來隨時查看！",
        hashtags=[
            "#nguoiVietODaiLoan",
            "#cuocSongDaiLoan",
            "#amThucDaiLoan",
            "#congDongNguoiViet",
            "#carryvietnamtw",
        ],
        slides=slides,
        media_dir=output_dir,
        is_mock=True,
        status=PostStatus.READY,
    )


def verify_carousel_directory(media_dir: Path, *, raise_on_error: bool = False) -> MediaValidationReport:
    """驗證目錄是否包含合規的六張獨立 1080×1350 JPEG 照片輪播。

    檢查項目：
    1. 目錄存在且為資料夾
    2. 拒絕目錄中出現任何非 JPEG 圖片檔（例如 .png, .webp, .gif）
    3. 圖片數量必須恰好為 6 張
    4. 檔名頁碼必須對應 01–06 順序無缺失
    5. 每個檔案二進位開頭必須為真實 JPEG magic bytes (FF D8 FF) 且 Pillow format == "JPEG"
    6. 每張圖片尺寸必須為 1080×1350
    7. 六張圖片的 SHA-256 不得重複（禁止複製同張圖片湊數）
    """
    resolved_dir = media_dir.expanduser().resolve()
    errors: list[str] = []
    items: list[MediaValidationItem] = []

    if not resolved_dir.exists() or not resolved_dir.is_dir():
        errors.append(f"媒體目錄不存在或不是資料夾: {resolved_dir}")
        report = MediaValidationReport(
            directory=str(resolved_dir),
            passed=False,
            actual_count=0,
            errors=errors,
        )
        if raise_on_error:
            raise MediaValidationError("媒體目錄驗證失敗", reasons=errors)
        return report

    all_files = sorted([p for p in resolved_dir.iterdir() if p.is_file() and not p.name.startswith(".")])

    # 檢查是否有非 .jpg/.jpeg 的圖片檔案（例如 PNG）
    forbidden_files = [
        p.name
        for p in all_files
        if p.suffix.lower() in {".png", ".webp", ".gif", ".bmp", ".mp4", ".mov"}
    ]
    if forbidden_files:
        errors.append(f"偵測到不允許的非 JPEG 媒體格式（如 PNG/影片）: {', '.join(forbidden_files)}")

    jpg_files = [p for p in all_files if p.suffix.lower() in {".jpg", ".jpeg"}]
    if len(jpg_files) != EXPECTED_SLIDE_COUNT:
        errors.append(f"JPEG 圖片數量必須恰好為 {EXPECTED_SLIDE_COUNT} 張，目前找到 {len(jpg_files)} 張")

    parsed_slides: list[tuple[int, Path]] = []
    for file_path in jpg_files:
        match = SLIDE_FILENAME_PATTERN.match(file_path.name)
        if not match:
            errors.append(
                f"檔名 '{file_path.name}' 不符合頁碼命名規範（應為 slide_01.jpg 至 slide_06.jpg）"
            )
            continue
        parsed_slides.append((int(match.group(1)), file_path))

    parsed_slides.sort(key=lambda x: x[0])
    found_indices = [idx for idx, _ in parsed_slides]
    if found_indices != [1, 2, 3, 4, 5, 6]:
        errors.append(f"輪播頁碼順序必須完整為 [1, 2, 3, 4, 5, 6]，實際解析為 {found_indices}")

    seen_hashes: dict[str, str] = {}

    for idx, file_path in parsed_slides:
        raw_bytes = file_path.read_bytes()
        file_size = len(raw_bytes)
        if file_size == 0:
            errors.append(f"檔案 '{file_path.name}' 為空檔案 (0 bytes)")
            continue

        # 檢查真實 JPEG Magic Bytes，防止將 PNG 改副檔名為 .jpg
        if not raw_bytes.startswith(JPEG_MAGIC_PREFIX):
            errors.append(
                f"檔案 '{file_path.name}' 並非真實 JPEG 格式（檔頭非 FF D8 FF，可能為偽裝副檔名之 PNG 或其他格式）"
            )
            continue

        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
        if sha256_hex in seen_hashes:
            errors.append(
                f"偵測到重複圖片：'{file_path.name}' 與 '{seen_hashes[sha256_hex]}' 內容完全相同 (SHA-256 碰撞)"
            )
        else:
            seen_hashes[sha256_hex] = file_path.name

        try:
            with Image.open(file_path) as img:
                img_format = img.format or "UNKNOWN"
                width, height = img.size
        except Exception as exc:
            errors.append(f"無法讀取圖片 '{file_path.name}': {exc}")
            continue

        if img_format.upper() != "JPEG":
            errors.append(f"圖片 '{file_path.name}' 真實格式為 {img_format}，必須為 JPEG")

        if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
            errors.append(
                f"圖片 '{file_path.name}' 尺寸為 {width}x{height}，不符合規範之 {EXPECTED_WIDTH}x{EXPECTED_HEIGHT} (4:5)"
            )

        items.append(
            MediaValidationItem(
                filename=file_path.name,
                path=str(file_path),
                index=idx,
                width=width,
                height=height,
                mime_type="image/jpeg" if img_format.upper() == "JPEG" else f"image/{img_format.lower()}",
                file_size_bytes=file_size,
                sha256=sha256_hex,
            )
        )

    passed = len(errors) == 0 and len(items) == EXPECTED_SLIDE_COUNT
    report = MediaValidationReport(
        directory=str(resolved_dir),
        passed=passed,
        expected_count=EXPECTED_SLIDE_COUNT,
        actual_count=len(jpg_files),
        items=items,
        errors=errors,
    )

    if not passed and raise_on_error:
        raise MediaValidationError("六張照片輪播媒體驗證未通過", reasons=errors)

    return report
