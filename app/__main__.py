"""允許使用 `python -m app <command>` 啟動 CLI。"""

from __future__ import annotations

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
