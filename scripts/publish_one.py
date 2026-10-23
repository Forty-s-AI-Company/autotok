"""在終端機發布單篇貼文（走 app.publisher.publish_post，閘門與網頁一致）。

只在這支腳本的程序內臨時開啟 ALLOW_LIVE_PUBLISH，不會改 .env，也不影響運作中的伺服器。
未通過審核的 App，TikTok 只允許「僅自己可見」，程式會自動只送 SELF_ONLY。

用法：
  uv run python scripts/publish_one.py POST_ID            # dry-run，不連 TikTok
  uv run python scripts/publish_one.py POST_ID --live     # 真的發布
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import AppSettings  # noqa: E402
from app.publisher import publish_post  # noqa: E402
from app.state import PostStateRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="發布單篇貼文")
    parser.add_argument("post_id")
    parser.add_argument("--live", action="store_true", help="真的發布（預設為 dry-run）")
    args = parser.parse_args()

    settings = AppSettings(ALLOW_LIVE_PUBLISH=True)  # dry-run 在連網前就會返回，所以兩種模式都可以安全開啟
    repo = PostStateRepository(settings.sqlite_db_path)
    account = repo.get_active_account()
    print(f"帳號：{account.handle}｜貼文：{args.post_id}｜模式：{'LIVE 真的發布' if args.live else 'dry-run'}")
    print(f"圖片網址前綴：{settings.tiktok_verified_image_url_prefix}")
    print(f"App 已審核：{settings.tiktok_app_audited}（False → 只會送 SELF_ONLY 僅自己可見）")
    t0 = time.time()
    outcome = publish_post(
        repo,
        settings,
        account.account_id,
        args.post_id,
        dry_run=not args.live,
        sleep=lambda s: (print(f"[{int(time.time() - t0):>3}s] 等待 TikTok 處理中…（{s}s）", flush=True), time.sleep(s))[1],
    )
    print(f"\n結果：{outcome.status}")
    print(f"說明：{outcome.detail}")
    if outcome.platform_post_id:
        print(f"TikTok ID：{outcome.platform_post_id}")
    return 0 if outcome.status in ("private", "published", "exported") else 1


if __name__ == "__main__":
    raise SystemExit(main())
