#!/bin/zsh
# macOS Finder 雙擊一鍵啟動腳本
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "================================================================"
echo " 🚀 正在啟動 Carry | Đài Loan × Việt Nam 圖形化一鍵控制台..."
echo " 🌐 瀏覽器將自動開啟 http://127.0.0.1:8765"
echo "================================================================"

uv run python run.py
