#!/usr/bin/env python3
"""啟動本機操作台：雙擊 `啟動控制台.command`（macOS）或 `啟動控制台.bat`（Windows）即可。

- 已經有一個新版操作台在跑：直接打開瀏覽器到它，不重複啟動。
- 預設的 31026 埠被別的程式（例如舊版）佔用：自動改用下一個空的埠，並打開瀏覽器到新位址。
命令列工具請用 `uv run autotok-vn --help`。
"""

from __future__ import annotations

import argparse
import json
import socket
import urllib.request
import webbrowser
from pathlib import Path

from app.config import AppSettings
from app.web.main import run_server


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def is_autotok(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=1.5) as r:  # noqa: S310
            return json.loads(r.read().decode()).get("app") == "autotok"
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="啟動 AutoTok 本機操作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=31026)
    parser.add_argument("--no-browser", action="store_true", help="不自動開啟瀏覽器")
    args = parser.parse_args()

    port = args.port
    for candidate in range(args.port, args.port + 20):
        if port_is_free(args.host, candidate):
            port = candidate
            break
        if is_autotok(args.host, candidate):
            url = f"http://{args.host}:{candidate}/"
            print(f"AutoTok 已經在執行：{url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
    else:
        print(f"{args.port} 到 {args.port + 19} 的埠都被佔用，請關閉其他程式後再試。")
        return 1
    if port != args.port:
        print(f"{args.port} 埠被其他程式佔用（可能是舊版），改用 {port} 埠。")

    root = Path(__file__).resolve().parent
    print(f"AutoTok 已啟動：http://{args.host}:{port}/（關閉這個視窗即結束）")
    run_server(root, AppSettings(), host=args.host, port=port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
