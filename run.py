#!/usr/bin/env python3
"""免背指令單一啟動器（`run.py`）。

支援兩種最直覺的使用方式：
1. 直接執行 `uv run python run.py`（或雙擊 `啟動控制台.command` / `啟動控制台.bat`）：
   - 預設直接啟動「Web 圖形化一鍵控制台 (`http://127.0.0.1:8765`)」並自動打開瀏覽器，在網頁上點按鈕即可生圖與同步 Notion。
2. 執行 `uv run python run.py --menu`：
   - 進入繁體中文互動式數字選單（按 1~5 即可操作，完全不必背任何指令參數）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from app.cli import main as cli_main
from app.config import AppSettings
from app.gui import run_gui_server


def run_interactive_menu() -> int:
    """終端機繁體中文數字選單（1~5）。"""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    while True:
        print("\n" + "=" * 64)
        print(" 🚀 Carry | Đài Loan × Việt Nam — 免背指令快速選單")
        print("=" * 64)
        print("  [1] 🌐 開啟「網頁圖形化一鍵控制台」（自動打開瀏覽器，最推薦！）")
        print(f"  [2] 🎨 一鍵生產明天 ({tomorrow}) 全日三時段雙語寫實圖 ＋ 同步至 Notion")
        print("  [3] 🎯 指定日期與單一時段（07:00 / 11:30 / 18:00）生圖 ＋ 同步至 Notion")
        print("  [4] 📊 執行每週市場研究與 Phase 5 成效權重優化報告")
        print("  [5] ❌ 離開")
        print("=" * 64)
        choice = input("請輸入數字 [1-5]（直接按 Enter 預設選 1）：").strip() or "1"

        if choice == "1":
            run_gui_server(open_browser=True)
            return 0
        if choice == "2":
            return cli_main(["sync-notion", "--date", tomorrow, "--slot", "all", "--engine", "codex"])
        if choice == "3":
            target_date = input(f"請輸入日期 YYYY-MM-DD（直接按 Enter 預設 {tomorrow}）：").strip() or tomorrow
            slot = input("請輸入時段 [all / 07:00 / 11:30 / 18:00]（預設 11:30）：").strip() or "11:30"
            return cli_main(["sync-notion", "--date", target_date, "--slot", slot, "--engine", "codex"])
        if choice == "4":
            cli_main(["seed-keywords"])
            cli_main(["research-batch", "--provider", "mock"])
            return cli_main(["analytics-report", "--mode", "mock"])
        if choice == "5":
            print("👋 再見！")
            return 0
        print("⚠️ 請輸入有效的選項數字 1 到 5。")


def main() -> int:
    project_root = Path(__file__).resolve().parent
    if "--menu" in sys.argv:
        return run_interactive_menu()
    # 預設直接啟動圖形化 Web 控制台並自動打開瀏覽器
    settings = AppSettings()
    run_gui_server(project_root=project_root, settings=settings, open_browser=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
