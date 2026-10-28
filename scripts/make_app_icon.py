"""產生 Carry Studio 的 App 圖示（1024x1024 PNG）。暖色系、疊起來的輪播卡片、不含任何 tik/tok 字樣，也不使用 TikTok 的品牌色。

用法：uv run python scripts/make_app_icon.py [輸出路徑]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SIZE = 1024
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/app_icon_carry_studio.png")


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def vertical_gradient(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    im = Image.new("RGB", (SIZE, SIZE))
    d = ImageDraw.Draw(im)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        d.line([(0, y), (SIZE, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom, strict=True)))
    return im


def card(fill: tuple[int, int, int], angle: float, offset: tuple[int, int], size: tuple[int, int] = (430, 560)) -> tuple[Image.Image, tuple[int, int]]:
    layer = Image.new("RGBA", (size[0] + 80, size[1] + 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((40, 40, 40 + size[0], 40 + size[1]), radius=56, fill=fill + (255,))
    layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    return layer, (SIZE // 2 - layer.width // 2 + offset[0], 380 - layer.height // 2 + offset[1])


def main() -> None:
    img = vertical_gradient((24, 28, 48), (58, 34, 40)).convert("RGBA")
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((180, 40, 860, 720), fill=(255, 168, 76, 90))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(120)))

    # 後兩張卡片（暖橘、淡金）＋前面一張深色卡片，表示輪播
    for fill, angle, off in (((232, 120, 72), 12, (70, 18)), ((250, 196, 112), 6, (36, 8))):
        layer, pos = card(fill, angle, off)
        shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        shadow.paste((0, 0, 0, 90), mask=layer.split()[3])
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)), (pos[0], pos[1] + 14))
        img.alpha_composite(layer, pos)
    front, pos = card((30, 36, 62), 0, (-20, 0))
    shadow = Image.new("RGBA", front.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), mask=front.split()[3])
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)), (pos[0], pos[1] + 18))
    img.alpha_composite(front, pos)

    # 前卡片上的小風景：太陽與山
    d = ImageDraw.Draw(img)
    x0, y0 = pos[0] + 40, pos[1] + 40
    d.ellipse((x0 + 70, y0 + 80, x0 + 170, y0 + 180), fill=(255, 190, 96))
    d.polygon([(x0 + 20, y0 + 470), (x0 + 170, y0 + 280), (x0 + 270, y0 + 390), (x0 + 330, y0 + 330), (x0 + 410, y0 + 470)], fill=(232, 120, 72))
    d.polygon([(x0 + 150, y0 + 470), (x0 + 250, y0 + 360), (x0 + 330, y0 + 470)], fill=(250, 196, 112))

    # 字樣
    text, f = "CARRY STUDIO", font(84)
    w = d.textlength(text, font=f)
    d.text(((SIZE - w) / 2, 800), text, font=f, fill=(255, 244, 228))
    sub, f2 = "Carousel workflow", font(40, bold=False)
    w2 = d.textlength(sub, font=f2)
    d.text(((SIZE - w2) / 2, 906), sub, font=f2, fill=(250, 196, 112))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(OUT, format="PNG", optimize=True)
    print(f"已產生：{OUT}（{SIZE}x{SIZE}，{OUT.stat().st_size // 1024} KB）")


if __name__ == "__main__":
    main()
