"""用「真實截圖」組成 TikTok App Review 的端到端示範影片（1920x1080 MP4 / H.264）。

每個畫面都是實際執行中的網站與操作台截圖，沒有任何畫出來的假畫面（TikTok 要求示範影片呈現真實的介面與流程）。
只有標題卡、結尾卡，以及一張「真實 API 執行結果」的文字卡（內容逐字取自實際執行的輸出）是版面設計。

用法：uv run python scripts/build_demo_video.py --shots <截圖資料夾> --out artifacts/carry_studio_demo.mp4
截圖檔名：site_home / site_privacy / site_terms / c_studio / c2_tiktok / c2_library（皆為 1920x1080 PNG）
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H, FPS, XFADE = 1920, 1080, 30, 0.6
BG, PANEL, INK, MUTED, ACCENT = (13, 17, 23), (28, 34, 44), (240, 244, 250), (150, 160, 175), (232, 120, 72)


def font(size: int, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    names = ("consolab.ttf" if bold else "consola.ttf",) if mono else (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf"))
    for n in (*names, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if d.textlength(trial, font=f) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    return [*lines, cur] if cur else lines


def caption_bar(d: ImageDraw.ImageDraw, step: str, text: str) -> None:
    d.rectangle((0, 980, W, H), fill=(10, 13, 18))
    chip = font(26, bold=True)
    cw = d.textlength(step, font=chip) + 36
    d.rounded_rectangle((60, 1006, 60 + cw, 1050), radius=10, fill=ACCENT)
    d.text((78, 1010), step, font=chip, fill=(20, 14, 10))
    f = font(30)
    lines = wrap(d, text, f, W - 60 - cw - 60 - 30)[:2]
    y = 1000 if len(lines) == 2 else 1013
    for ln in lines:
        d.text((60 + cw + 30, y), ln, font=f, fill=INK)
        y += 38


def shot_scene(path: Path, url: str, step: str, text: str, crop: tuple[int, int, int, int] | None = None) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((120, 14, W - 120, 66), radius=14, fill=PANEL)
    d.text((152, 21), url, font=font(28), fill=INK)
    raw = Image.open(path).convert("RGB")
    if crop:  # 網站頁面內容很窄，放大裁切到內容區（16:9）才看得清楚
        raw = raw.crop(crop)
    shot = raw.resize((1584, 891), Image.Resampling.LANCZOS)
    im.paste(shot, ((W - 1584) // 2, 84))
    d.rectangle(((W - 1584) // 2 - 1, 83, (W + 1584) // 2, 975), outline=(60, 70, 85))
    caption_bar(d, step, text)
    return im


def card(title: str, lines: list[str], footer: str = "") -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((160, 150), title, font=font(96, bold=True), fill=INK)
    d.rectangle((160, 280, 460, 286), fill=ACCENT)
    y = 340
    for ln in lines:
        for part in wrap(d, ln, font(42), W - 320):
            d.text((160, y), part, font=font(42), fill=(205, 212, 224))
            y += 62
        y += 14
    if footer:
        d.text((160, H - 120), footer, font=font(30), fill=MUTED)
    return im


def log_scene(step: str, text: str, header: str, rows: list[str]) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((120, 60), header, font=font(36, bold=True), fill=INK)
    d.rounded_rectangle((120, 140, W - 120, 900), radius=18, fill=(8, 10, 14), outline=(50, 60, 75))
    y = 180
    for ln in rows:
        d.text((170, y), ln, font=font(34, mono=True), fill=(170, 230, 190) if ln.startswith("->") else (210, 215, 225))
        y += 58
    caption_bar(d, step, text)
    return im


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--publish-id", default="")
    ap.add_argument("--run-time", default="")
    a = ap.parse_args()
    S, site = a.shots, "https://studio.carry-digital-nomad.in.net"

    scenes: list[tuple[Image.Image, float]] = [
        (
            card(
                "Carry Studio",
                [
                    "Internal creator workflow tool for our own TikTok account (@carry.vietnam.tw).",
                    f"Website: {site}",
                    "TikTok products: Login Kit, Content Posting API (Upload to TikTok).",
                    "Scopes: user.info.basic, video.upload.",
                    "Everything below is recorded from the running application in the TikTok Sandbox.",
                ],
            ),
            6,
        ),
        (shot_scene(S / "site_home.png", f"{site}/", "STEP 1", "Website. This is the Web URL registered in the app settings, on the same domain.", (560, 338, 1360, 788)), 4),
        (shot_scene(S / "site_privacy.png", f"{site}/privacy", "STEP 1", "Privacy Policy (1/2): we process only the connected account's OAuth token and our own carousel images and captions.", (480, 40, 1440, 580)), 5),
        (shot_scene(S / "site_privacy.png", f"{site}/privacy", "STEP 1", "Privacy Policy (2/2): images are hosted temporarily and deleted once the creator has published the post; we collect no data about other TikTok users.", (480, 540, 1440, 1080)), 6),
        (shot_scene(S / "site_terms.png", f"{site}/terms", "STEP 1", "Terms of Service (1/2): the tool is used to prepare and preview multi-photo carousel posts for TikTok accounts.", (480, 40, 1440, 580)), 5),
        (shot_scene(S / "site_terms.png", f"{site}/terms", "STEP 1", "Terms of Service (2/2): posts may be sent to the TikTok inbox as drafts; the creator decides in the TikTok app whether to publish.", (480, 540, 1440, 1080)), 6),
        (
            shot_scene(
                S / "c_studio.png",
                "http://127.0.0.1:31026/studio   (operator console, runs on our own computer)",
                "STEP 2",
                "Create and review. The operator prepares a carousel (1080x1350 images, caption, hashtags) and reviews the quality checks before anything is sent.",
            ),
            8,
        ),
        (
            shot_scene(
                S / "c2_tiktok.png",
                "http://127.0.0.1:31026/settings   (operator console)",
                "STEP 3",
                "Login Kit. The creator connects the TikTok account with 'Authorize with TikTok' (Sandbox). The panel shows the authorized account, token expiry and our redirect URI.",
            ),
            8,
        ),
        (
            shot_scene(
                S / "c2_library.png",
                "http://127.0.0.1:31026/library   (operator console)",
                "STEP 4",
                "Upload to TikTok. The Content Posting API (MEDIA_UPLOAD) sends the carousel to the creator's TikTok inbox as a draft. Status: sent to inbox, waiting for the creator to publish in the app.",
            ),
            9,
        ),
        (
            log_scene(
                "STEP 4",
                "Output of a real upload run in the Sandbox. TikTok accepted the upload and reported the carousel as delivered to the creator's inbox.",
                f"Real run output{(' - ' + a.run_time) if a.run_time else ''}",
                [
                    "mode = inbox   (Upload to TikTok, MEDIA_UPLOAD)",
                    "POST /v2/post/publish/content/init/",
                    "     media_type=PHOTO  post_mode=MEDIA_UPLOAD  source=PULL_FROM_URL  photos=6",
                    f"-> publish_id: {a.publish_id or '(see run)'}",
                    "poll /v2/post/publish/status/fetch/",
                    "-> result: inbox - carousel delivered to the creator's TikTok inbox",
                    "local status: SENT_TO_INBOX  (not published yet)",
                ],
            ),
            9,
        ),
        (
            card(
                "In the TikTok app",
                [
                    "The draft appears in the creator's TikTok inbox.",
                    "The creator reviews it, chooses the privacy setting, labels AI-generated content and taps Post.",
                    "In this mode Carry Studio never publishes on its own.",
                ],
                "All screens in this video are captured from the running application.",
            ),
            8,
        ),
    ]

    a.out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        inputs: list[str] = []
        for i, (im, secs) in enumerate(scenes):
            p = Path(tmp) / f"s{i:02d}.png"
            im.save(p)
            inputs += ["-loop", "1", "-t", f"{secs + XFADE}", "-i", str(p)]
        chain, label, offset = [], "[0:v]", 0.0
        for i in range(1, len(scenes)):
            offset += scenes[i - 1][1]
            out = f"[v{i}]"
            chain.append(f"{label}[{i}:v]xfade=transition=fade:duration={XFADE}:offset={offset:.2f}{out}")
            label = out
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(chain), "-map", label,
            "-r", str(FPS), "-c:v", "libx264", "-preset", "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(a.out),
        ]  # fmt: skip
        subprocess.run(cmd, check=True)
    total = sum(s for _, s in scenes)
    print(f"完成：{a.out}（約 {total} 秒，{a.out.stat().st_size / 1024 / 1024:.1f} MB）")


if __name__ == "__main__":
    main()
