"""Phase 3｜越南文六頁輪播內容企劃（Editorial Agent）、分離式視覺渲染（Visual Production & Renderer）與品質 Gate 重試管線。

遵循 MASTER_SPEC.md 第 2, 3, 4, 6 節規範：
1. **Editorial Agent (`EditorialPlanner`)**：
   - 依每日固定三時段（`Asia/Taipei`）：
     * `07:00`（家庭／夢想／情緒／成長，對應 Notion 分類：`家庭情緒`）
     * `11:30`（台灣生活／文化／美食／旅行，對應 Notion 分類：`台灣生活`）
     * `18:00`（手機拍攝／創作／網路行銷／數位成長，對應 Notion 分類：`圖文行銷`）
   - 產出越南文六頁腳本、繁體中文逐頁翻譯對照、越南文標題（<=90 字元）、貼文內文、核心切角、CTA 與**恰好五個簡明相關 hashtag**。
   - **合規風險審核 (`audit_editorial_compliance`)**：嚴禁虛構職缺、保證高薪、簽證保證或未揭露限制之商業承諾。
2. **分離式背景生成 (`SceneImageProvider`) 與文字排版渲染 (`SlideTypographyRenderer`)**：
   - 圖片背景生成與越南文文字排版嚴格分離（禁止依賴 AI 模型直接在圖片內亂生偽文字）。
   - `SceneImageProvider`：支援替換為外部寫實場景生成模型；在未配置付費圖像 API 時提供高解析漸層景深＋每頁專屬多樣構圖光影基底（6 張構圖色溫與光影焦點各自獨立，絕不共用單一平面背景＋通用圖示模板）。
   - `SlideTypographyRenderer`：使用支援越南文聲調（ă, â, đ, ê, ô, ơ, ư 及五種聲調符號）與繁體中文的合法系統字型（如 Arial Unicode / PingFang / DejaVuSans），在安全區（Safe Zone，避開頂部狀態列、右側互動按鈕列與底部文案遮擋區）精確疊加編號 `01–06`、越南文主文案與品牌浮水印 `Carry | Đài Loan × Việt Nam`。
   - 同時支援渲染對應的「繁體中文對照版圖片」（6 張 1080×1350 JPEG），以完整對應使用者真實 Notion 資料庫中的 `越南文圖片` 與 `中文版圖片` 雙欄位！
3. **Quality Gate 與問題頁限次重製 (`produce_carousel_with_qa_retry`)**：
   - 驗證尺寸 `1080×1350`、真實 JPEG MIME、編號 `01–06`、越南文编码完整性、安全區邊界與 6 張圖視覺多樣性（檢查像素平均值與直方圖差異，防止全組同模板或重複圖）。
   - 若單頁或整體品質不合格，記錄原因並針對問題頁限次重製（預設 `max_retries=2`）；重試後仍未達合格則將貼文標記為 `PostStatus.QUALITY_FAILED`，嚴禁進入發布排程。
"""

from __future__ import annotations

import hashlib
import math
import subprocess
import textwrap
from abc import ABC, abstractmethod
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from app.errors import AutotokSystemError, MediaValidationError
from app.media import EXPECTED_HEIGHT, EXPECTED_WIDTH, verify_carousel_directory
from app.models import (
    CarouselPostPackage,
    ContentSlot,
    MediaValidationReport,
    PostStatus,
    SlideItem,
)

# 禁止出現之違規或誇大承諾詞彙（MASTER_SPEC.md 第 2, 6 節：不虛構職缺、簽證資格、保證收入）
FORBIDDEN_CLAIMS_PATTERNS: list[str] = [
    "cam kết lương cao",
    "bao đậu visa",
    "việc nhẹ lương cao",
    "làm giàu nhanh",
    "保證高薪",
    "保證過簽",
    "免工作證合法打工",
    "保證月入十萬",
]

# 對應真實 Notion 資料庫 `分類` 欄位的三個選項與 `時段` 選項
SLOT_TO_NOTION_CATEGORY: dict[ContentSlot, tuple[str, str]] = {
    ContentSlot.MORNING_0700: ("07:00", "家庭情緒"),
    ContentSlot.NOON_1130: ("11:30", "台灣生活"),
    ContentSlot.EVENING_1800: ("18:00", "圖文行銷"),
}

# 安全區（Safe Zone）定義：避開 TikTok 手機介面頂部頁籤、右側按讚分享列與底部標題區
SAFE_ZONE_LEFT = 84
SAFE_ZONE_RIGHT = EXPECTED_WIDTH - 130  # 右側預留 130px 避開 TikTok 頭像/愛心/收藏圖示
SAFE_ZONE_TOP = 110
SAFE_ZONE_BOTTOM = EXPECTED_HEIGHT - 160


def audit_editorial_compliance(texts: list[str]) -> list[str]:
    """審核文案是否包含虛構職缺、保證簽證或誇大收入等違規字眼。"""
    violations: list[str] = []
    combined = "\n".join(texts).lower()
    for phrase in FORBIDDEN_CLAIMS_PATTERNS:
        if phrase.lower() in combined:
            violations.append(f"偵測到違反品牌營運原則之誇大或敏感詞彙: '{phrase}'")
    return violations


def load_vietnamese_capable_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """載入支援完整越南文聲調符號與繁體中文的字型。"""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msjh.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        p = Path(candidate)
        if p.is_file():
            try:
                return ImageFont.truetype(str(p), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


class SceneImageProvider(ABC):
    """背景場景圖片產生器抽象介面（與文字排版分離）。"""

    provider_name: str

    @abstractmethod
    def render_background(
        self,
        *,
        slot: ContentSlot,
        slide_index: int,
        attempt: int = 1,
    ) -> Image.Image:
        """生成單頁 1080×1350 無文字之場景背景底圖。"""


def resize_and_crop_to_1080x1350(img_path: Path, out_jpg_path: Path) -> None:
    """將 ChatGPT Codex CLI 生成之高解析 PNG 等比例置中裁切並輸出為 1080×1350 真實 JPEG。"""
    with Image.open(img_path) as im:
        rgb = im.convert("RGB")
        fitted = ImageOps.fit(
            rgb,
            (EXPECTED_WIDTH, EXPECTED_HEIGHT),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        out_jpg_path.parent.mkdir(parents=True, exist_ok=True)
        fitted.save(out_jpg_path, format="JPEG", quality=95, optimize=True)


class ChatGPTCodexCliCarouselGenerator:
    """呼叫本機 ChatGPT / Codex CLI (`/Applications/ChatGPT.app/Contents/Resources/codex`)
    內建之 `image_gen` 工具，生成具備真實台灣生活場景攝影與雜誌級排版的 6 張越南文輪播圖與 6 張繁體中文對照圖。
    """

    CODEX_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    DEFAULT_MODEL = "gpt-6-astra"

    # 針對每個時段與每一頁提供具體寫實的台灣場景描述，確保 6 張圖風格統一但場景構圖完全不同
    SLIDE_SCENE_BRIEFS: dict[ContentSlot, list[str]] = {
        ContentSlot.MORNING_0700: [
            "Warm golden evening light in a cozy Taiwanese apartment near a window overlooking Taipei street lights at dusk, a cup of hot tea and a smartphone on a wooden desk, warm editorial photography",
            "Close-up warm lifestyle photo of a young Vietnamese worker or student's hands holding a smartphone on a video call table setup with a warm notebook and lamp in Taiwan, comforting mood",
            "Editorial flat-lay on a warm wooden desk in Taiwan: a clean monthly savings notebook, New Taiwan Dollar (NTD) budget planner cards, a pen, and a ceramic coffee mug, natural morning sunlight",
            "A bright cozy study corner in Taiwan with Traditional Chinese-Vietnamese vocabulary flashcards, headphones, and a tablet on a wooden table, soft natural daylight",
            "Warm weekend gathering table in Taiwan with authentic Vietnamese dishes, fresh herbs, and Taiwanese fruit tea shared among friends, candid editorial food & lifestyle photography",
            "Inspiring morning sunrise view over a Taiwanese city street with scooters and breakfast shops waking up, warm hopeful cinematic lighting, clean negative space at top for CTA text",
        ],
        ContentSlot.NOON_1130: [
            "Sunlit Taiwanese 7-Eleven / FamilyMart convenience store counter with a steaming hot roasted sweet potato in paper bag, a City Cafe cup with Taipei 101 illustration, and a colorful EasyCard (悠遊卡) on the counter, photorealistic editorial photography",
            "Close-up photorealistic shot of a colorful Taiwan EasyCard (悠遊卡) tapping on a Taipei MRT turnstile gate with modern station lighting in the background, crisp depth of field",
            "Photorealistic scene inside a bright Taiwanese convenience store showing neatly organized parcel pickup shelves and a friendly counter package box (店到店取貨), warm clean lighting",
            "Photorealistic close-up of a modern touchscreenmultimedia kiosk (ibon / FamiPort style) inside a Taiwan convenience store with a smartphone and freshly printed A4 document, crisp editorial style",
            "Appetizing editorial food photography of Taiwanese convenience store lunch favorites: marbled tea eggs (茶葉蛋), golden roasted sweet potato, and iced tea on a clean counter by the window",
            "Warm inviting twilight exterior view of a glowing Taiwanese convenience store on a lively Taipei street corner, cinematic editorial photography with clean upper area for final CTA",
        ],
        ContentSlot.EVENING_1800: [
            "Warm editorial creator workspace flat-lay: a smartphone on a small tripod, a laptop showing a 6-slide 4:5 carousel grid, coffee cup, and caramel-orange workflow cards on a cream desk",
            "Close-up over-the-shoulder shot of a smartphone screen capturing a vertical 4:5 photo of a lively Taiwanese street scene, grid lines visible on camera screen, crisp natural lighting",
            "Clean editorial split-layout desk scene showing 4 numbered storyboards and authentic Taiwan lifestyle photos arranged neatly on a warm textured paper surface",
            " Clean mobile UI mockup held in hand showing TikTok vertical 4:5 safe zone margins (avoiding right-side heart/save icons and bottom caption area), crisp studio lighting",
            "Close-up of a laptop and smartphone displaying crisp Vietnamese typography with full diacritics on a 1080x1350 vertical slide, accompanied by a color palette card and stylus",
            "Minimalist warm editorial creator desk with a glowing notebook and smartphone displaying '@carry.vietnam.tw', caramel orange accent card, clean composition for final save & follow CTA",
        ],
    }

    def __init__(self, *, codex_bin: Path | None = None, model: str | None = None) -> None:
        self.codex_bin = codex_bin or self.CODEX_BIN
        self.model = model or self.DEFAULT_MODEL

    def is_available(self) -> bool:
        return self.codex_bin.is_file()

    def generate_slot_via_codex(
        self,
        *,
        slot: ContentSlot,
        vi_out_dir: Path,
        zh_out_dir: Path,
        workspace_dir: Path,
    ) -> None:
        """透過 ChatGPT Codex CLI `image_gen` 生成該時段的 6 張越南文輪播圖與 6 張繁體中文對照圖。"""
        if not self.is_available():
            raise AutotokSystemError(
                f"找不到 ChatGPT Codex CLI 執行檔: {self.codex_bin}",
                code="CODEX_CLI_NOT_FOUND",
            )

        spec = DailySlotEditorialSpec.SPECS[slot]
        raw_slides: list[tuple[str, str, str]] = list(spec["slides"])  # type: ignore[arg-type]
        scene_briefs = self.SLIDE_SCENE_BRIEFS[slot]

        raw_png_dir = vi_out_dir.parent / "raw_codex_png"
        raw_png_dir.mkdir(parents=True, exist_ok=True)
        vi_out_dir.mkdir(parents=True, exist_ok=True)
        zh_out_dir.mkdir(parents=True, exist_ok=True)

        # 將 6 頁分為兩批（1–3 頁、4–6 頁）呼叫 Codex CLI，每批透過 `image_gen` 產出 6 張寫實圖片（3 越 + 3 中），兼顧穩定度與速度
        batches = [
            (1, 2, 3),
            (4, 5, 6),
        ]

        for batch_indices in batches:
            missing_in_batch = [
                idx
                for idx in batch_indices
                if not (raw_png_dir / f"vi_{idx:02d}.png").is_file()
                or not (raw_png_dir / f"zh_{idx:02d}.png").is_file()
            ]
            if not missing_in_batch:
                continue

            slide_instructions: list[str] = []
            for idx in missing_in_batch:
                vi_head, vi_body, zh_full = raw_slides[idx - 1]
                scene_desc = scene_briefs[idx - 1]
                zh_parts = zh_full.split("：", 1)
                zh_head = zh_parts[0]
                zh_body = zh_parts[1] if len(zh_parts) > 1 else zh_full
                vi_png = (raw_png_dir / f"vi_{idx:02d}.png").resolve()
                zh_png = (raw_png_dir / f"zh_{idx:02d}.png").resolve()

                slide_instructions.append(
                    f"""
### Slide {idx:02d}/06
- Photorealistic Taiwan Scene: {scene_desc}
- Editorial Layout: Warm sunlit magazine-grade photography, top-left small brand label 'Carry | Đài Loan × Việt Nam', bottom progress dots at position {idx}/6 with text '{idx}/6', bottom-left 'SHARE', bottom-right 'SAVE'.
- Step A (Vietnamese image): Call `image_gen` with exact on-image headline "{vi_head}" and concise subtitle "{vi_body}". Copy the generated PNG to `{vi_png}`.
- Step B (Traditional Chinese image): Call `image_gen` with the SAME Taiwan scene style and exact on-image Traditional Chinese headline "{zh_head}" and subtitle "{zh_body}". Copy the generated PNG to `{zh_png}`.
"""
                )

            prompt = (
                "You are generating photorealistic TikTok 4:5 vertical carousel slides for '@carry.vietnam.tw' "
                "(Brand: 'Carry | Đài Loan × Việt Nam').\n"
                "CRITICAL RULES:\n"
                "1. You MUST use your built-in `image_gen` tool for EVERY requested image below.\n"
                "2. NEVER use Python/Pillow/ImageMagick/SVG/HTML to synthesize or draw images or text overlays. Let `image_gen` natively render both the photorealistic Taiwan photography AND the crisp editorial typography inside the image.\n"
                "3. Keep safe margins on the right side and bottom.\n"
                "4. Immediately after each `image_gen` call, copy the generated PNG file from `~/.codex/generated_images/...` to the exact target path specified.\n\n"
                + "\n".join(slide_instructions)
            )

            cmd = [
                str(self.codex_bin),
                "exec",
                "-m",
                self.model,
                "-C",
                str(workspace_dir),
                "-s",
                "danger-full-access",
                prompt,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise AutotokSystemError(
                    f"ChatGPT Codex CLI 執行失敗 (exit={proc.returncode}): {proc.stderr[-500:] or proc.stdout[-500:]}",
                    code="CODEX_EXEC_FAILED",
                )

        # 將 6 張越文 PNG 與 6 張中文 PNG 轉存為精確 1080×1350 JPEG
        for idx in range(1, 7):
            vi_png = raw_png_dir / f"vi_{idx:02d}.png"
            zh_png = raw_png_dir / f"zh_{idx:02d}.png"
            if not vi_png.is_file() or not zh_png.is_file():
                raise AutotokSystemError(
                    f"Codex CLI 未完整產出第 {idx:02d} 頁圖片 ({vi_png.name} / {zh_png.name})",
                    code="CODEX_MISSING_OUTPUT",
                )
            resize_and_crop_to_1080x1350(vi_png, vi_out_dir / f"slide_{idx:02d}.jpg")
            resize_and_crop_to_1080x1350(zh_png, zh_out_dir / f"slide_{idx:02d}.jpg")


class ProceduralAtmosphericSceneProvider(SceneImageProvider):
    """多樣化景深光影背景產生器（無需外部付費 API 時之高品質擬真光影基底，標記 is_mock=True）。

    針對 07:00（晨光溫暖人物情緒色調）、11:30（台灣街景夜市豐富光影色調）、18:00（創作者工作檯現代科技色調）
    以及第 1 至第 6 頁採用不同的焦點構圖（左焦點、右焦點、中心景深、對角光束、雙層地平線、黃金時刻漸層），
    確保 6 張圖風格一致但構圖與像素分佈顯著多樣，絕非單一純色平面背景。
    """

    provider_name = "atmospheric_scene_mock"

    SLOT_THEMES: dict[ContentSlot, list[tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]]] = {
        ContentSlot.MORNING_0700: [
            ((36, 28, 46), (112, 68, 78), (238, 172, 114)),
            ((28, 38, 56), (88, 92, 118), (228, 182, 132)),
            ((44, 32, 38), (128, 78, 72), (242, 196, 138)),
            ((26, 44, 52), (76, 108, 112), (218, 178, 126)),
            ((48, 36, 52), (118, 82, 98), (244, 188, 142)),
            ((32, 42, 62), (96, 102, 128), (246, 206, 136)),
        ],
        ContentSlot.NOON_1130: [
            ((18, 48, 58), (42, 112, 108), (244, 184, 72)),
            ((46, 32, 24), (138, 72, 44), (248, 198, 88)),
            ((22, 54, 42), (56, 124, 88), (236, 204, 96)),
            ((38, 36, 64), (86, 88, 138), (242, 168, 92)),
            ((52, 28, 36), (142, 64, 76), (246, 202, 108)),
            ((20, 46, 68), (52, 108, 142), (248, 192, 78)),
        ],
        ContentSlot.EVENING_1800: [
            ((16, 24, 42), (38, 68, 112), (96, 212, 228)),
            ((24, 20, 46), (68, 52, 118), (132, 196, 246)),
            ((18, 32, 38), (42, 92, 104), (112, 228, 196)),
            ((28, 24, 48), (78, 64, 124), (238, 164, 98)),
            ((14, 28, 46), (46, 84, 128), (108, 218, 236)),
            ((22, 26, 52), (62, 76, 136), (148, 224, 248)),
        ],
    }

    def render_background(
        self,
        *,
        slot: ContentSlot,
        slide_index: int,
        attempt: int = 1,
    ) -> Image.Image:
        palettes = self.SLOT_THEMES.get(slot, self.SLOT_THEMES[ContentSlot.NOON_1130])
        top_rgb, mid_rgb, glow_rgb = palettes[(slide_index - 1) % len(palettes)]

        # 以縮小比例快速計算平滑多層光影，再放大為 1080×1350 並套用高斯景深（Bokeh）
        scale = 6
        sw, sh = EXPECTED_WIDTH // scale, EXPECTED_HEIGHT // scale
        small = Image.new("RGB", (sw, sh))
        pixels = small.load()

        # 每頁焦點座標不同，確保 6 張構圖各自獨立
        focus_x = sw * (0.22 + 0.11 * ((slide_index * 2 + attempt) % 5))
        focus_y = sh * (0.25 + 0.10 * (slide_index % 4))
        max_dist = math.hypot(sw, sh) * 0.65

        for y in range(sh):
            ty = y / max(sh - 1, 1)
            base_r = top_rgb[0] * (1 - ty) + mid_rgb[0] * ty
            base_g = top_rgb[1] * (1 - ty) + mid_rgb[1] * ty
            base_b = top_rgb[2] * (1 - ty) + mid_rgb[2] * ty

            for x in range(sw):
                dist = math.hypot(x - focus_x, y - focus_y)
                glow = max(0.0, 1.0 - (dist / max_dist)) ** 1.8
                r = int(min(255, base_r * (1 - glow * 0.55) + glow_rgb[0] * (glow * 0.55)))
                g = int(min(255, base_g * (1 - glow * 0.55) + glow_rgb[1] * (glow * 0.55)))
                b = int(min(255, base_b * (1 - glow * 0.55) + glow_rgb[2] * (glow * 0.55)))
                pixels[x, y] = (r, g, b)

        bg = small.resize((EXPECTED_WIDTH, EXPECTED_HEIGHT), resample=Image.Resampling.BICUBIC)
        draw = ImageDraw.Draw(bg, "RGBA")

        # 依頁碼繪製不同景深光斑（Bokeh circles）與建築／街景環境地平線層次，避免呆板模板感
        for i in range(4):
            cx = int((EXPECTED_WIDTH * ((slide_index * 17 + i * 29 + attempt * 7) % 80 + 10)) / 100)
            cy = int((EXPECTED_HEIGHT * ((slide_index * 23 + i * 31) % 70 + 15)) / 100)
            radius = 90 + (i * 45) + (slide_index * 12)
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=(glow_rgb[0], glow_rgb[1], glow_rgb[2], 38 - i * 6),
            )

        bg = bg.filter(ImageFilter.GaussianBlur(radius=14))
        return bg


class SlideTypographyRenderer:
    """分離式越南文／繁體中文安全區文字排版渲染器。"""

    def render_slide(
        self,
        background: Image.Image,
        *,
        slide_index: int,
        total_slides: int = 6,
        slot: ContentSlot,
        headline_text: str,
        body_text: str,
        subtitle_note: str,
        language_label: str = "VI",
    ) -> Image.Image:
        """在安全區內將編號 01–06、主標題、重點內文與品牌識別疊加至場景底圖上。"""
        canvas = background.copy().convert("RGBA")
        overlay = Image.new("RGBA", (EXPECTED_WIDTH, EXPECTED_HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        time_str, category_zh = SLOT_TO_NOTION_CATEGORY[slot]

        # 每頁卡片高度與佈局位置依頁碼微調（封面頁、重點頁、尾頁 CTA 視覺節奏變化）
        if slide_index == 1:
            card_top, card_bottom = 320, 1080
        elif slide_index == total_slides:
            card_top, card_bottom = 360, 1060
        else:
            offset = (slide_index % 3) * 35
            card_top, card_bottom = 290 + offset, 1040 + offset

        # 半透明毛玻璃質感閱讀襯底（嚴格限制在 SAFE_ZONE 內）
        draw.rounded_rectangle(
            [SAFE_ZONE_LEFT, card_top, SAFE_ZONE_RIGHT, card_bottom],
            radius=32,
            fill=(14, 20, 30, 198),
            outline=(255, 255, 255, 75),
            width=3,
        )

        # 頂部品牌與頁碼膠囊標章（位於安全區頂端）
        badge_font = load_vietnamese_capable_font(28)
        num_font = load_vietnamese_capable_font(46)
        title_font = load_vietnamese_capable_font(42)
        body_font = load_vietnamese_capable_font(33)
        footer_font = load_vietnamese_capable_font(25)

        draw.rounded_rectangle(
            [SAFE_ZONE_LEFT, SAFE_ZONE_TOP, SAFE_ZONE_LEFT + 170, SAFE_ZONE_TOP + 68],
            radius=18,
            fill=(245, 188, 66, 235),
        )
        draw.text(
            (SAFE_ZONE_LEFT + 28, SAFE_ZONE_TOP + 10),
            f"{slide_index:02d} / {total_slides:02d}",
            fill=(18, 22, 30, 255),
            font=num_font,
        )

        draw.text(
            (SAFE_ZONE_LEFT + 195, SAFE_ZONE_TOP + 18),
            f"Carry | Đài Loan × Việt Nam  •  {time_str} {category_zh} [{language_label}]",
            fill=(245, 248, 252, 235),
            font=badge_font,
        )

        # 卡片內部文字排版（自動換行確保絕不超出右側安全區）
        text_left = SAFE_ZONE_LEFT + 44
        cursor_y = card_top + 48

        wrapped_headline = textwrap.wrap(headline_text, width=22 if language_label == "ZH" else 28)
        for line in wrapped_headline[:3]:
            draw.text((text_left, cursor_y), line, fill=(248, 204, 88, 255), font=title_font)
            cursor_y += 58

        cursor_y += 20
        draw.line(
            [(text_left, cursor_y), (SAFE_ZONE_RIGHT - 44, cursor_y)],
            fill=(255, 255, 255, 65),
            width=2,
        )
        cursor_y += 32

        wrapped_body = textwrap.wrap(body_text, width=24 if language_label == "ZH" else 34)
        for line in wrapped_body[:6]:
            draw.text((text_left, cursor_y), line, fill=(255, 255, 255, 248), font=body_font)
            cursor_y += 50

        if subtitle_note:
            cursor_y = max(cursor_y + 24, card_bottom - 115)
            wrapped_sub = textwrap.wrap(subtitle_note, width=28 if language_label == "ZH" else 38)
            for line in wrapped_sub[:2]:
                draw.text((text_left, cursor_y), line, fill=(185, 215, 238, 230), font=footer_font)
                cursor_y += 36

        # 底部安全區內帳號提示
        draw.text(
            (SAFE_ZONE_LEFT + 10, SAFE_ZONE_BOTTOM - 45),
            "@carry.vietnam.tw  |  Lưu lại & Chia sẻ cho bạn bè tại Đài Loan",
            fill=(240, 240, 240, 215),
            font=footer_font,
        )

        combined = Image.alpha_composite(canvas, overlay).convert("RGB")
        return combined


class DailySlotEditorialSpec:
    """每日三時段（07:00 / 11:30 / 18:00）完整越南文與繁體中文對照內容模板庫。"""

    SPECS: dict[ContentSlot, dict[str, object]] = {
        ContentSlot.MORNING_0700: {
            "vi_title": "6 cách giữ tinh thần vững vàng khi làm việc và học tập xa nhà ở Đài Loan",
            "zh_tw_title": "在台異鄉打拼必看：保持安定心態與家人溫暖連結的 6 個習慣",
            "core_angle": "從在台越南青年的下班獨處情緒切入，提供具體可行的家庭聯繫與個人成長步驟",
            "cta": "Lưu lại bài viết và bình luận điều bạn nhớ nhất ở quê nhà nhé!",
            "vi_caption": (
                "Sống và làm việc tại Đài Loan đôi khi sẽ có những buổi chiều thật nhớ nhà. "
                "Đây là 6 thói quen nhỏ giúp mình giữ vững tinh thần, vừa chăm sóc bản thân vừa làm gia đình yên tâm."
            ),
            "zh_tw_caption": (
                "在台灣生活與工作，總有幾個傍晚特別想家。整理了 6 個幫助自己穩定心情、照顧好身體也讓家人安心的小習慣。"
            ),
            "hashtags": [
                "#nguoiVietODaiLoan",
                "#nguoiVietNhoNha",
                "#cuocSongDaiLoan",
                "#congDongNguoiViet",
                "#carryvietnamtw",
            ],
            "slides": [
                (
                    "01. Xa nhà nhưng không cô đơn tại Đài Loan",
                    "Những buổi chiều tan ca ở Đài Loan, ai cũng từng chạnh lòng nhớ bữa cơm gia đình. Hãy thử 6 thói quen nhỏ này để mỗi ngày đều có thêm động lực.",
                    "01. 在台灣異鄉打拼，但你並不孤單：每次下班想家時，試試這 6 個讓自己更有力量的日常習慣。",
                ),
                (
                    "02. Đặt lịch gọi video cố định với gia đình",
                    "Chỉ cần 15 phút mỗi tối cuối tuần kể chuyện đời thường ở Đài Loan cũng đủ giúp bố mẹ ở Việt Nam yên tâm hơn rất nhiều.",
                    "02. 與家人約定固定的視訊時間：每週固定 15 分鐘分享台灣日常，就能讓越南的父母安心。",
                ),
                (
                    "03. Ghi chép rõ ràng chi tiêu và mục tiêu tiết kiệm",
                    "Khi nhìn thấy khoản tích lũy tăng lên từng tháng một cách minh bạch, bạn sẽ thấy công sức làm việc tại Đài Loan thật sự xứng đáng.",
                    "03. 清楚記錄每月開支與儲蓄目標：看見努力工作累積的成果，會讓異鄉打拼更有踏實感。",
                ),
                (
                    "04. Dành 20 phút mỗi ngày học thêm tiếng Trung giao tiếp",
                    "Biết thêm vài câu giao tiếp tại siêu thị, tàu điện hay nơi làm việc giúp bạn tự tin và giảm hẳn cảm giác xa lạ.",
                    "04. 每天花 20 分鐘學幾句實用生活中文：在超商、捷運或職場能自然溝通，會大幅減少陌生與焦慮感。",
                ),
                (
                    "05. Kết nối với cộng đồng người Việt tích cực, hợp pháp",
                    "Cùng bạn bè nấu món Việt hoặc đi dạo cuối tuần giúp giải tỏa căng thẳng và luôn nhắc nhau tuân thủ quy định tại Đài Loan.",
                    "05. 參與正向且合法的在台越南社群：週末和同鄉朋友聚餐散步、互相提醒台灣法規，彼此扶持更長久。",
                ),
                (
                    "06. Lưu lại & Theo dõi @carry.vietnam.tw",
                    "Chúc bạn luôn bình an và vững bước tại Đài Loan! Hãy lưu bài viết và theo dõi Carry để cùng nhau phát triển mỗi ngày nhé.",
                    "06. 收藏並追蹤 @carry.vietnam.tw：祝你在台灣平安順心！先收藏本篇並追蹤我們，每天一起成長。",
                ),
            ],
        },
        ContentSlot.NOON_1130: {
            "vi_title": "6 dịch vụ cực tiện tại cửa hàng tiện lợi Đài Loan người Việt nên biết",
            "zh_tw_title": "在台越南人必收！台灣超商 6 個超神實用功能全整理",
            "core_angle": "將台灣 7-11 與全家超商的萬能服務拆解成 6 頁實用步驟，解決日常繳費與寄件痛點",
            "cta": "Lưu ngay để dùng khi ra 7-Eleven hoặc FamilyMart nhé!",
            "vi_caption": (
                "Cửa hàng tiện lợi ở Đài Loan không chỉ bán đồ ăn mà còn giúp giải quyết đủ việc trong ngày. "
                "Cùng điểm qua 6 tiện ích thiết thực nhất cho người Việt tại Đài Loan nhé!"
            ),
            "zh_tw_caption": (
                "台灣的便利商店不只賣食物，還能幫你搞定各種生活大小事！一起來看看在台越南朋友最常用的 6 個超商實用服務。"
            ),
            "hashtags": [
                "#cuocSongDaiLoan",
                "#nguoiVietTaiDaiLoan",
                "#amThucDaiLoan",
                "#kinhNghiemDaiLoan",
                "#carryvietnamtw",
            ],
            "slides": [
                (
                    "01. Cửa hàng tiện lợi Đài Loan làm được gì?",
                    "Ở Đài Loan, chỉ cần bước vào 7-Eleven hoặc FamilyMart cách nhà vài trăm mét là bạn có thể xử lý ngay 6 việc quan trọng này.",
                    "01. 台灣便利商店到底有多萬能？只要走進巷口的 7-11 或全家，就能立刻辦好這 6 件生活重要事項。",
                ),
                (
                    "02. Nạp tiền và mua thẻ EasyCard (Du Du Khả)",
                    "Đưa thẻ tại quầy thu ngân để nạp tiền mặt trong 10 giây, dùng đi tàu MRT, xe buýt và thanh toán cực nhanh.",
                    "02. 悠遊卡（EasyCard）購買與十秒現金加值：直接在櫃檯加值，搭捷運、公車與小額付款一卡搞定。",
                ),
                (
                    "03. Gửi và nhận hàng giữa các cửa hàng (Điếm đáo điếm)",
                    "Không sợ vắng nhà khi giao hàng! Bạn có thể chọn nhận bưu phẩm 24/7 tại cửa hàng tiện lợi gần chỗ ở nhất.",
                    "03. 店到店寄件與 24 小時取貨：上班上課不怕錯過宅配，直接選離宿舍最近的超商隨時領取包裹。",
                ),
                (
                    "04. In tài liệu, ảnh thẻ từ điện thoại tại máy Kiosk",
                    "Tải file lên mã QR tại máy ibon hoặc FamiPort để in hồ sơ, giấy tờ học tập và làm việc chỉ với vài Đài tệ.",
                    "04. 用手機上傳檔案到機台列印文件與證件照：透過 ibon 或 FamiPort 掃碼，幾塊錢台幣就能印好重要文件。",
                ),
                (
                    "05. Bữa trưa nhanh gọn: Khoai lang nướng & Trứng trà",
                    "Những món nóng hổi, sạch sẽ và giá hợp lý tại quầy giúp bạn nạp năng lượng nhanh vào giờ nghỉ trưa.",
                    "05. 高 CP 值熱食補給：烤地瓜、茶葉蛋與微波便當，午休時間快速衛生又有飽足感的好選擇。",
                ),
                (
                    "06. Lưu lại ngay & Theo dõi @carry.vietnam.tw",
                    "Bạn hay dùng dịch vụ nào nhất ở cửa hàng tiện lợi Đài Loan? Hãy lưu bài viết và chia sẻ cho bạn mới sang nhé!",
                    "06. 立即收藏並追蹤 @carry.vietnam.tw：你最常在台灣超商使用哪個功能？快收藏並分享給剛來台灣的朋友！",
                ),
            ],
        },
        ContentSlot.EVENING_1800: {
            "vi_title": "6 bước chụp và thiết kế bài đăng 6 trang bằng điện thoại tại Đài Loan",
            "zh_tw_title": "用手機記錄台灣生活：6 步驟做出高收藏率六頁圖文輪播",
            "core_angle": "教導在台越南青年如何用手機實拍日常並整理成結構清晰的 6 頁知識圖卡",
            "cta": "Lưu lại công thức 6 trang này để áp dụng cho bài đăng tiếp theo của bạn!",
            "vi_caption": (
                "Không cần máy ảnh đắt tiền, bạn hoàn toàn có thể dùng điện thoại để chia sẻ kinh nghiệm sống tại Đài Loan "
                "qua định dạng 6 ảnh dọc 4:5 rõ ràng và thu hút."
            ),
            "zh_tw_caption": (
                "不需要昂貴相機，只要用手機搭配這套 6 頁 4:5 直式圖文結構，就能清楚分享你的台灣生活經驗與創作！"
            ),
            "hashtags": [
                "#sangTaoNoiDung",
                "#chupAnhDienThoai",
                "#nguoiVietODaiLoan",
                "#phatTrienBanThan",
                "#carryvietnamtw",
            ],
            "slides": [
                (
                    "01. Công thức làm bài 6 trang thu hút trên TikTok",
                    "Định dạng 6 ảnh dọc 4:5 (1080x1350) giúp người xem dễ lướt đọc từng ý và lưu lại khi cần tra cứu.",
                    "01. TikTok 高收藏六頁輪播公式：採用 4:5 (1080x1350) 六張獨立直圖，讓讀者輕鬆滑動閱讀並一鍵收藏。",
                ),
                (
                    "02. Trang 1: Nêu rõ vấn đề hoặc con số cụ thể",
                    "Hãy đặt tiêu đề ngắn gọn, đánh trúng mối quan tâm thực tế của người Việt tại Đài Loan ngay từ trang bìa.",
                    "02. 第一頁封面：用具體數字與明確痛點開場，一眼抓住在台越南受眾真正關心的生活主題。",
                ),
                (
                    "03. Trang 2–5: Mỗi trang chỉ tập trung 1 ý chính",
                    "Đừng nhồi nhét quá nhiều chữ! Hãy chia nhỏ thành từng bước thực tế kèm bối cảnh đời sống thật tại Đài Loan.",
                    "03. 第二至五頁：一頁只講清楚一個核心步驟，搭配真實台灣街景或工作生活情境，避免文字過載。",
                ),
                (
                    "04. Chú ý vùng an toàn (Safe Zone) khi đặt chữ",
                    "Chừa khoảng trống bên phải và phía dưới để chữ tiếng Việt không bị nút thả tim hay phần mô tả của TikTok che khuất.",
                    "04. 嚴格留出安全區（Safe Zone）：避開右側愛心收藏按鈕與底部文案區，確保越南文聲調符號清晰不裁切。",
                ),
                (
                    "05. Kiểm tra dấu tiếng Việt và độ nét JPEG",
                    "Luôn dùng phông chữ hỗ trợ đầy đủ dấu tiếng Việt và xuất file JPEG 1080x1350 sắc nét trước khi đăng.",
                    "05. 檢查越南文聲調字型與 JPEG 畫質：使用完整支援越文聲調的字體，並輸出 1080x1350 獨立高清 JPEG。",
                ),
                (
                    "06. Trang 6: Lời kêu gọi Lưu bài & Theo dõi",
                    "Khép lại bằng lời nhắc lưu bài viết và theo dõi @carry.vietnam.tw để cùng học hỏi kỹ năng số mỗi ngày!",
                    "06. 第六頁尾頁：明確引導讀者收藏與追蹤 @carry.vietnam.tw，每天一起累積數位創作實力！",
                ),
            ],
        },
    }


def verify_visual_diversity_and_text_integrity(
    package: CarouselPostPackage,
    media_dir: Path,
) -> tuple[bool, list[str]]:
    """進階視覺品質 Gate：檢查越南文文案合規性、各頁文字非空且不重複、以及 6 張圖像素多樣性。"""
    errors: list[str] = []

    # 1. 檢查合規詞彙
    all_texts = [package.vi_title, package.vi_caption] + [s.vi_text for s in package.slides]
    errors.extend(audit_editorial_compliance(all_texts))

    # 2. 檢查 6 頁越南文與繁體中文翻譯是否齊全且各頁文字不重複
    vi_texts = [s.vi_text.strip() for s in package.slides]
    zh_texts = [s.zh_tw_translation.strip() for s in package.slides]
    if len(set(vi_texts)) != 6:
        errors.append("六頁越南文圖中文字出現重複內容，違反每頁獨立重點規範。")
    if len(set(zh_texts)) != 6:
        errors.append("六頁繁體中文對照翻譯出現重複內容。")

    # 3. 檢查 6 張圖片之間的像素平均色彩差異（防止全組共用同一死板背景）
    mean_vectors: list[tuple[float, float, float]] = []
    for idx in range(1, 7):
        img_path = media_dir / f"slide_{idx:02d}.jpg"
        if not img_path.is_file():
            errors.append(f"缺少 slide_{idx:02d}.jpg")
            continue
        with Image.open(img_path) as im:
            stat = ImageStat.Stat(im.convert("RGB"))
            mean_vectors.append((stat.mean[0], stat.mean[1], stat.mean[2]))

    for i in range(len(mean_vectors)):
        for j in range(i + 1, len(mean_vectors)):
            dist = math.dist(mean_vectors[i], mean_vectors[j])
            if dist < 2.5:
                errors.append(
                    f"視覺多樣性不足：slide_{i + 1:02d}.jpg 與 slide_{j + 1:02d}.jpg 色彩與構圖過度相似 (色差距離 {dist:.2f} < 2.5)"
                )

    return len(errors) == 0, errors


def produce_carousel_with_qa_retry(
    *,
    slot: ContentSlot,
    post_id: str,
    scheduled_date_str: str,
    output_base_dir: Path,
    scene_provider: SceneImageProvider | None = None,
    use_codex_cli: bool = False,
    codex_generator: ChatGPTCodexCliCarouselGenerator | None = None,
    max_retries: int = 2,
    simulate_first_attempt_corrupt_slide: int | None = None,
) -> tuple[CarouselPostPackage, MediaValidationReport, Path]:
    """生產單篇六頁越南文輪播圖（含同步產出繁體中文對照圖），並執行 Quality Gate 與問題頁限次重製。

    若 `use_codex_cli=True`，將優先使用本機 ChatGPT Codex CLI (`/Applications/ChatGPT.app/Contents/Resources/codex`)
    內建的 `image_gen` 生成雜誌級寫實台灣生活攝影輪播圖與繁體中文對照圖，並裁切為 1080×1350 JPEG。
    回傳 `(CarouselPostPackage, MediaValidationReport, zh_reference_media_dir)`。
    若重試 `max_retries` 次後仍不合格，將 `package.status` 標記為 `QUALITY_FAILED` 並拋出 `MediaValidationError`。
    """
    provider = scene_provider or ProceduralAtmosphericSceneProvider()
    renderer = SlideTypographyRenderer()
    spec = DailySlotEditorialSpec.SPECS[slot]
    time_str, _ = SLOT_TO_NOTION_CATEGORY[slot]

    vi_dir = (output_base_dir / post_id / "vi").resolve()
    zh_dir = (output_base_dir / post_id / "zh_tw").resolve()
    vi_dir.mkdir(parents=True, exist_ok=True)
    zh_dir.mkdir(parents=True, exist_ok=True)

    raw_slides: list[tuple[str, str, str]] = list(spec["slides"])  # type: ignore[arg-type]
    slides_items: list[SlideItem] = []

    if use_codex_cli:
        codex_gen = codex_generator or ChatGPTCodexCliCarouselGenerator()
        # 若目標目錄已經有完整 6 張越文與 6 張中文 JPEG（例如剛由 Codex 生成完成），直接驗證並包裝，避免重複耗時生成
        existing_vi_ok = verify_carousel_directory(vi_dir, raise_on_error=False).passed
        existing_zh_ok = verify_carousel_directory(zh_dir, raise_on_error=False).passed
        if not (existing_vi_ok and existing_zh_ok):
            codex_gen.generate_slot_via_codex(
                slot=slot,
                vi_out_dir=vi_dir,
                zh_out_dir=zh_dir,
                workspace_dir=Path.cwd(),
            )
        for idx, (vi_head, vi_body, zh_full) in enumerate(raw_slides, start=1):
            vi_path = vi_dir / f"slide_{idx:02d}.jpg"
            slides_items.append(
                SlideItem(
                    index=idx,
                    vi_text=f"{vi_head} — {vi_body}",
                    zh_tw_translation=zh_full,
                    image_path=vi_path,
                    sha256=hashlib.sha256(vi_path.read_bytes()).hexdigest(),
                )
            )
        is_mock_flag = False
    else:
        for idx, (vi_head, vi_body, zh_full) in enumerate(raw_slides, start=1):
            bg = provider.render_background(slot=slot, slide_index=idx, attempt=1)

            # 渲染越南文正式圖 (1080×1350 JPEG)
            vi_img = renderer.render_slide(
                bg,
                slide_index=idx,
                slot=slot,
                headline_text=vi_head,
                body_text=vi_body,
                subtitle_note=f"Đối chiếu ZH: {zh_full[:44]}...",
                language_label="VI",
            )
            vi_path = vi_dir / f"slide_{idx:02d}.jpg"

            # 支援在測試中模擬第一次產生時某單頁尺寸錯誤，驗證問題頁自動限次重製機制
            if simulate_first_attempt_corrupt_slide == idx:
                bad_img = vi_img.resize((800, 800))
                bad_img.save(vi_path, format="JPEG", quality=90)
            else:
                vi_img.save(vi_path, format="JPEG", quality=94, optimize=True)

            # 同步渲染繁體中文對照版圖片（供 Notion `中文版圖片` 欄位與審稿使用）
            zh_parts = zh_full.split("：", 1)
            zh_head = zh_parts[0]
            zh_body = zh_parts[1] if len(zh_parts) > 1 else zh_full
            zh_img = renderer.render_slide(
                bg,
                slide_index=idx,
                slot=slot,
                headline_text=zh_head,
                body_text=zh_body,
                subtitle_note=f"VI: {vi_head}",
                language_label="ZH",
            )
            zh_path = zh_dir / f"slide_{idx:02d}.jpg"
            zh_img.save(zh_path, format="JPEG", quality=92, optimize=True)

            slides_items.append(
                SlideItem(
                    index=idx,
                    vi_text=f"{vi_head} — {vi_body}",
                    zh_tw_translation=zh_full,
                    image_path=vi_path,
                    sha256=hashlib.sha256(vi_path.read_bytes()).hexdigest(),
                )
            )
        is_mock_flag = provider.provider_name.endswith("mock")

    package = CarouselPostPackage(
        post_id=post_id,
        slot=slot,
        scheduled_at_taipei=f"{scheduled_date_str}T{time_str}:00+08:00",
        vi_title=str(spec["vi_title"]),
        zh_tw_title=str(spec["zh_tw_title"]),
        vi_caption=str(spec["vi_caption"]),
        zh_tw_caption=str(spec["zh_tw_caption"]),
        hashtags=list(spec["hashtags"]),  # type: ignore[arg-type]
        slides=slides_items,
        media_dir=vi_dir,
        is_mock=is_mock_flag,
        status=PostStatus.GENERATING,
    )

    # 執行 Quality Gate 驗證，若失敗則針對問題頁限次重製
    for attempt in range(1, max_retries + 2):
        base_report = verify_carousel_directory(vi_dir, raise_on_error=False)
        div_passed, div_errors = verify_visual_diversity_and_text_integrity(package, vi_dir)
        combined_errors = base_report.errors + div_errors

        if base_report.passed and div_passed:
            package.status = PostStatus.READY
            return package, base_report, zh_dir

        if attempt > max_retries:
            package.status = PostStatus.QUALITY_FAILED
            base_report.passed = False
            base_report.errors = combined_errors
            raise MediaValidationError(
                f"QUALITY_FAILED：經過 {max_retries} 次重試後仍未通過六張圖品質 Gate",
                reasons=combined_errors,
            )

        # 找出需要重製的問題頁碼（若無法定位單頁則重製全組）
        problem_indices: set[int] = set()
        for err in combined_errors:
            for idx in range(1, 7):
                if f"slide_{idx:02d}.jpg" in err:
                    problem_indices.add(idx)
        if not problem_indices:
            problem_indices = {1, 2, 3, 4, 5, 6}

        for idx in sorted(problem_indices):
            vi_head, vi_body, zh_full = raw_slides[idx - 1]
            bg = provider.render_background(slot=slot, slide_index=idx, attempt=attempt + 1)
            vi_img = renderer.render_slide(
                bg,
                slide_index=idx,
                slot=slot,
                headline_text=vi_head,
                body_text=vi_body,
                subtitle_note=f"Đối chiếu ZH: {zh_full[:44]}...",
                language_label="VI",
            )
            vi_path = vi_dir / f"slide_{idx:02d}.jpg"
            vi_img.save(vi_path, format="JPEG", quality=94, optimize=True)
            package.slides[idx - 1].sha256 = hashlib.sha256(vi_path.read_bytes()).hexdigest()

    raise AutotokSystemError("未預期的品質檢查流程結束", code="QUALITY_FAILED")
