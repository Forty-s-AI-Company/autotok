#!/bin/zsh
# macOS 雙擊啟動
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
uv run python run.py
