"""在終端機製作一篇貼文：與網頁「製作」相同的流程（即時文案 → Codex 生圖 → 品質檢查 → 存進資料庫），
並把 Codex CLI 的即時輸出印出來，方便觀看進度。

用法：uv run python scripts/produce_topic.py [--date YYYY-MM-DD] [--count N]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.editorial as editorial  # noqa: E402
import app.production as production  # noqa: E402
from app.config import AppSettings  # noqa: E402
from app.editorial import TopicBrief  # noqa: E402
from app.models import ContentSlot  # noqa: E402
from app.pipeline import produce_post  # noqa: E402
from app.state import PostStateRepository  # noqa: E402

TOPIC = "想家的時候，在台灣也能吃到的家鄉味"
POINTS = [
    "在台灣超市和越南店就能買到的食材",
    "三道簡單的家鄉味做法",
    "一個人吃飯也不孤單的小儀式",
    "邀請留言分享你最想念的越南菜",
]
T0 = time.time()


def stamp() -> str:
    return f"[{int(time.time() - T0):>4}s]"


def streaming_codex(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """與 app.engines.run_codex_cmd 相同（含 Windows 多行提示改走標準輸入），但會即時印出 Codex 的輸出。"""
    stdin_text = None
    if os.name == "nt" and cmd and "\n" in cmd[-1]:
        stdin_text = cmd[-1]
        cmd = [*cmd[:-1], "-"]
    print(f"{stamp()} 啟動 Codex：{Path(cmd[0]).name} exec …（提示 {len(stdin_text or cmd[-1])} 字）", flush=True)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out: list[str] = []
    err: list[str] = []

    def pump(stream, buf: list[str], tag: str) -> None:  # noqa: ANN001
        for line in stream:
            buf.append(line)
            text = line.rstrip()
            if text:
                print(f"{stamp()} {tag} {text[:180]}", flush=True)

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, out, "[codex out]"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, err, "[codex log]"), daemon=True),
    ]
    for t in threads:
        t.start()
    if stdin_text is not None:
        proc.stdin.write(stdin_text)
        proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    for t in threads:
        t.join(timeout=5)
    print(f"{stamp()} Codex 結束（exit={proc.returncode}）", flush=True)
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "".join(err))


def main() -> int:
    parser = argparse.ArgumentParser(description="在終端機製作一篇貼文（Codex 文案＋生圖）")
    parser.add_argument("--date", default="2026-10-25", help="發布日期 YYYY-MM-DD")
    parser.add_argument("--count", type=int, default=None, help="張數 3–10（預設依內容決定）")
    args = parser.parse_args()

    editorial.run_codex_cmd = streaming_codex  # type: ignore[assignment]
    production.run_codex_cmd = streaming_codex  # type: ignore[assignment]

    cfg = AppSettings()
    repo = PostStateRepository(cfg.sqlite_db_path)
    account = repo.get_active_account()
    print(f"帳號：{account.handle}｜模型：{cfg.codex_model}｜日期：{args.date}｜11:30 生活與文化")
    print(f"主題：{TOPIC}")
    pkg = produce_post(
        repo,
        cfg,
        ROOT,
        account_id=account.account_id,
        date_str=args.date,
        slot=ContentSlot.NOON_1130,
        engine="codex",
        count=args.count,
        brief=TopicBrief(title=TOPIC, points=POINTS),
        progress=lambda msg: print(f"{stamp()} ▶ {msg}", flush=True),
    )
    print(f"\n完成：{pkg.post_id}｜狀態：{pkg.status.value}｜模擬資料：{pkg.is_mock}")
    print(f"越文標題：{pkg.vi_title}")
    print(f"繁中標題：{pkg.zh_tw_title}")
    print(f"圖片資料夾：{pkg.media_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
