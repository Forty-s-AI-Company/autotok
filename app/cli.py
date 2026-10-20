"""命令列介面（CLI）入口。

提供以下子命令（支援 `python -m app <command>`）：
1. `generate-mock-media <dir>`：生成 6 張 1080×1350、標記 TEST/MOCK 的獨立測試 JPEG（01–06）。
2. `verify-media <dir>`：嚴格檢查目錄內是否為 6 張獨立 1080×1350 真實 JPEG、無 PNG、無重複圖、順序 01–06。
3. `publish`：執行六張照片輪播發布流程（預設 `--dry-run` 輸出安全發布計畫；未設定 `ALLOW_LIVE_PUBLISH=true` 與明確授權前，阻擋任何真實公開發布）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.adapters import get_uploader_adapter
from app.config import AppSettings, redact_sensitive
from app.errors import AutotokSystemError
from app.media import generate_mock_carousel_media, verify_carousel_directory
from app.state import PostStateRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="TikTok 越南市場 AI 成長與自動化發布系統（Phase 1 CLI）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. generate-mock-media
    gen_parser = sub.add_parser(
        "generate-mock-media",
        help="生成 6 張可重現、標記 TEST/MOCK 的 1080x1350 獨立 JPEG 測試圖（順序 01–06）",
    )
    gen_parser.add_argument(
        "output_dir",
         nargs="?",
        default="artifacts/mock_media/post_001",
        help="輸出目錄路徑（預設：artifacts/mock_media/post_001）",
    )
    gen_parser.add_argument(
        "--post-id",
        default="mock-post-001",
        help="測試貼文識別碼（預設：mock-post-001）",
    )

    # 2. verify-media
    verify_parser = sub.add_parser(
        "verify-media",
        help="檢查目標目錄是否包含合規的 6 張 1080x1350 真實 JPEG 照片輪播（01–06）",
    )
    verify_parser.add_argument(
        "media_dir",
        help="待驗證之六張照片目錄路徑",
    )

    # 3. publish
    pub_parser = sub.add_parser(
        "publish",
        help="執行或預覽六張照片輪播發布計畫（預設要求 --dry-run；真實發布預設禁用）",
    )
    pub_parser.add_argument(
        "--media-dir",
        default="artifacts/mock_media/post_001",
        help="六張 JPEG 所在目錄（若不存在且為 dry-run，將自動生成 mock 測試圖）",
    )
    pub_parser.add_argument(
        "--post-id",
        default="mock-post-001",
        help="貼文識別碼（預設：mock-post-001）",
    )
    pub_parser.add_argument(
        "--adapter",
        choices=["mock", "official_api", "autotok_candidate", "manual_export"],
        default=None,
        help="指定發布通道 Adapter（預設讀取 DEFAULT_UPLOADER_ADAPTER 或 mock）",
    )
    pub_parser.add_argument(
        "--post-mode",
        choices=["DIRECT_POST", "MEDIA_UPLOAD"],
        default="DIRECT_POST",
        help="官方 API 發布模式：DIRECT_POST（直接發布）或 MEDIA_UPLOAD（送入草稿/收件匣）",
    )
    mode_group = pub_parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="僅輸出安全的發文計畫與媒體驗證結果，不產生任何外部副作用",
    )
    mode_group.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="嘗試執行正式發布（必須同時設定環境變數 ALLOW_LIVE_PUBLISH=true 與有效憑證，否則會被安全攔截）",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = AppSettings()

    try:
        if args.command == "generate-mock-media":
            out_dir = Path(args.output_dir)
            pkg = generate_mock_carousel_media(out_dir, post_id=args.post_id)
            repo = PostStateRepository(settings.sqlite_db_path)
            repo.upsert_post(pkg)
            output = {
                "action": "generate-mock-media",
                "post_id": pkg.post_id,
                "media_dir": str(pkg.media_dir),
                "slide_count": len(pkg.slides),
                "slides": [
                    {
                        "index": s.index,
                        "file": s.image_path.name if s.image_path else None,
                        "sha256": s.sha256,
                    }
                    for s in pkg.slides
                ],
                "status": pkg.status.value,
            }
            print(redact_sensitive(json.dumps(output, ensure_ascii=False, indent=2)))
            return 0

        if args.command == "verify-media":
            report = verify_carousel_directory(Path(args.media_dir), raise_on_error=False)
            print(redact_sensitive(report.model_dump_json(indent=2)))
            return 0 if report.passed else 1

        if args.command == "publish":
            # 若未明確傳入 --dry-run 且未傳入 --live，則若 ALLOW_LIVE_PUBLISH 未啟用時立即阻擋
            if not args.dry_run and not args.live:
                print(
                    json.dumps(
                        {
                            "error": "BLOCKED_LIVE_PUBLISH_DISABLED",
                            "message": "正式 publish 預設禁用。請加上 `--dry-run` 輸出安全發文計畫，或在獲得授權後設定 ALLOW_LIVE_PUBLISH=true 並搭配 `--live`。",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    file=sys.stderr,
                )
                return 2

            is_dry_run = bool(args.dry_run and not args.live)
            media_dir = Path(args.media_dir)

            # 若目錄尚不存在且為 dry-run，自動建立可重現的 6 張測試 JPEG
            if is_dry_run and not media_dir.exists():
                pkg = generate_mock_carousel_media(media_dir, post_id=args.post_id)
            else:
                # 讀取既有目錄並建立測試 Package 物件以供驗證與排程預覽
                pkg = generate_mock_carousel_media(media_dir, post_id=args.post_id) if not media_dir.exists() else _package_from_existing_dir(media_dir, post_id=args.post_id)

            adapter_name = args.adapter or settings.default_uploader_adapter
            adapter = get_uploader_adapter(adapter_name, settings=settings)

            plan = adapter.publish_carousel(
                pkg,
                dry_run=is_dry_run,
                post_mode=args.post_mode,
            )

            repo = PostStateRepository(settings.sqlite_db_path)
            pkg.status = plan.status
            repo.upsert_post(pkg)

            print(redact_sensitive(plan.model_dump_json(indent=2)))
            return 0

    except AutotokSystemError as exc:
        err_payload = {
            "error_code": exc.code,
            "message": str(exc),
        }
        print(redact_sensitive(json.dumps(err_payload, ensure_ascii=False, indent=2)), file=sys.stderr)
        return 2

    return 0


def _package_from_existing_dir(media_dir: Path, post_id: str):
    """從既有目錄載入並包裝為 CarouselPostPackage（不覆寫既有圖片檔案）。"""
    from app.media import DEFAULT_MOCK_SLIDES
    from app.models import CarouselPostPackage, ContentSlot, PostStatus, SlideItem

    slides = [
        SlideItem(
            index=i,
            vi_text=DEFAULT_MOCK_SLIDES[i - 1][0],
            zh_tw_translation=DEFAULT_MOCK_SLIDES[i - 1][1],
            image_path=(media_dir / f"slide_{i:02d}.jpg").resolve(),
        )
        for i in range(1, 7)
    ]
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
        media_dir=media_dir.resolve(),
        is_mock=True,
        status=PostStatus.READY,
    )


if __name__ == "__main__":
    raise SystemExit(main())
