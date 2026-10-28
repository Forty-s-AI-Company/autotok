"""只重畫一篇既有貼文的圖片，文案（draft.json）原封不動；即時顯示 Codex 輸出。

用途：生圖規則改了（例如拿掉圓點），而貼文還沒發布時，用新規則重畫。舊圖會先備份到 `<貼文資料夾>_備份`。
只能重畫狀態是 READY 的貼文，已發布的不能動。

用法：uv run python scripts/regenerate_images.py --date 2026-10-24 --slot 0700
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from produce_topic import stamp, streaming_codex  # noqa: E402

import app.editorial as editorial  # noqa: E402
import app.production as production  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.editorial import EditorialDraft  # noqa: E402
from app.models import ContentSlot, PostStatus  # noqa: E402
from app.pipeline import draft_path, make_post_id  # noqa: E402
from app.production import ChatGPTCodexCliCarouselGenerator, produce_carousel_with_qa_retry  # noqa: E402
from app.state import PostStateRepository  # noqa: E402

SLOTS = {"0700": ContentSlot.MORNING_0700, "1130": ContentSlot.NOON_1130, "1800": ContentSlot.EVENING_1800}


def main() -> int:
    parser = argparse.ArgumentParser(description="用新規則重畫既有貼文的圖片（文案不變）")
    parser.add_argument("--date", required=True, help="貼文日期 YYYY-MM-DD")
    parser.add_argument("--slot", required=True, choices=sorted(SLOTS), help="內容線代碼")
    args = parser.parse_args()
    slot = SLOTS[args.slot]

    editorial.run_codex_cmd = streaming_codex  # type: ignore[assignment]
    production.run_codex_cmd = streaming_codex  # type: ignore[assignment]

    cfg = AppSettings()
    repo = PostStateRepository(cfg.sqlite_db_path)
    account = repo.get_active_account()
    market = repo.get_market_profile(account.account_id)
    post_id = make_post_id(account.account_id, args.date, slot)
    status = repo.get_post_status(post_id)
    if status != PostStatus.READY:
        print(f"{post_id} 的狀態是 {status.value if status else '不存在'}，只有 READY 的貼文可以重畫。")
        return 1
    draft_file = draft_path(ROOT, args.date, post_id)
    if not draft_file.is_file():
        print(f"找不到文案草稿 {draft_file}，無法保留原文案。")
        return 1
    draft = EditorialDraft.model_validate_json(draft_file.read_text(encoding="utf-8"))

    out_base = ROOT / "artifacts" / "daily_production" / args.date
    post_dir = out_base / post_id
    backup = out_base / f"{post_id}_備份"
    work = out_base / "_重畫暫存"  # 先在這裡完整畫完；成功才整組換上去，中途失敗或被中斷時原本的圖完全不動
    for stale in (backup, work):
        if stale.exists():
            shutil.rmtree(stale)
    print(f"帳號：{account.handle}｜貼文：{post_id}（{market.time_for(slot)}）｜模型：{cfg.codex_model}")

    pkg, _, _ = produce_carousel_with_qa_retry(
        slot=slot,
        post_id=post_id,
        scheduled_date_str=args.date,
        output_base_dir=work,
        account_id=account.account_id,
        account_handle=account.handle,
        use_codex_cli=True,
        codex_generator=ChatGPTCodexCliCarouselGenerator(model=cfg.codex_model, sandbox=cfg.codex_image_sandbox),
        draft=draft,
        market=market,
        brand_label=account.display_name,
    )

    # 成功：保留文案草稿，舊資料夾改名備份，新資料夾換上去，再更新資料庫裡的路徑
    shutil.copy2(draft_file, work / post_id / "draft.json")
    post_dir.rename(backup)
    (work / post_id).rename(post_dir)
    shutil.rmtree(work, ignore_errors=True)
    new_vi = post_dir / "vi"
    final = pkg.model_copy(
        update={
            "media_dir": new_vi,
            "slides": [s.model_copy(update={"image_path": new_vi / f"slide_{s.index:02d}.jpg"}) for s in pkg.slides],
        }
    )
    repo.upsert_post(final)
    print(f"舊圖已備份到：{backup}")
    print(f"\n[{stamp()}] 完成：{final.post_id}｜狀態：{final.status.value}｜{len(final.slides)} 張")
    print(f"圖片資料夾：{final.media_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
