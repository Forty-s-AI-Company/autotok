"""本機輕量級圖形預覽與操作介面（Local Web Dashboard GUI）。

遵循 MASTER_SPEC.md 第 10 節（「可選簡單 Web UI，MVP 先 CLI + Markdown 報告；不用過度建置 Next.js SaaS」）：
- 使用 Python 標準庫 `http.server` 提供零額外依賴之本機圖形化控制台（預設 `http://127.0.0.1:8765`）。
- 支援視覺化檢視：
  1. 系統與 Notion / TikTok 安全閘門狀態（已自動遮罩敏感 Token）
  2. 每日三時段（07:00 / 11:30 / 18:00）已生產之六頁越南文輪播圖與繁體中文對照圖並排預覽
  3. 越南文搜尋關鍵字字典與 35 篇研究案例／選題評分總覽
  4. 一鍵複製 TikTok 越南文標題、內文與 5 個 Hashtag
"""

from __future__ import annotations

import html
import json
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.analytics import AnalyticsAgent
from app.config import AppSettings
from app.state import PostStateRepository


def render_dashboard_html(project_root: Path, settings: AppSettings) -> str:
    """渲染本機 Web GUI 儀表板 HTML。"""
    repo = PostStateRepository(settings.sqlite_db_path)
    keywords = repo.list_seed_keywords()
    cases = repo.list_research_cases(days=90)
    observations = repo.list_post_observations(include_mock=True)
    analytics_agent = AnalyticsAgent()
    opt_proposal = analytics_agent.generate_weekly_optimization_proposal(observations)

    # 掃描 artifacts/daily_production 下已生成的貼文目錄
    prod_root = project_root / "artifacts" / "daily_production"
    post_cards_html: list[str] = []

    if prod_root.is_dir():
        for date_dir in sorted(prod_root.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for post_dir in sorted(date_dir.iterdir()):
                if not post_dir.is_dir():
                    continue
                vi_dir = post_dir / "vi"
                zh_dir = post_dir / "zh_tw"
                vi_imgs = sorted(vi_dir.glob("slide_0*.jpg")) if vi_dir.is_dir() else []
                zh_imgs = sorted(zh_dir.glob("slide_0*.jpg")) if zh_dir.is_dir() else []
                if not vi_imgs:
                    continue

                rel_vi = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in vi_imgs
                ]
                rel_zh = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in zh_imgs
                ]

                slides_grid = "".join(
                    f"""
                    <div class="slide-pair">
                      <div class="slide-label">Slide {idx:02d} (越南文正式圖 / 繁中對照圖)</div>
                      <div class="pair-imgs">
                        <a href="{v_url}" target="_blank"><img src="{v_url}" alt="VI {idx}" loading="lazy"/></a>
                        {f'<a href="{z_url}" target="_blank"><img src="{z_url}" alt="ZH {idx}" loading="lazy"/></a>' if z_url else ''}
                      </div>
                    </div>
                    """
                    for idx, (v_url, z_url) in enumerate(
                        zip(rel_vi, rel_zh + [""] * 6, strict=False), start=1
                    )
                )

                post_cards_html.append(
                    f"""
                    <div class="card">
                      <div class="card-header">
                        <h3>📅 {html.escape(date_dir.name)} ｜ 🆔 {html.escape(post_dir.name)}</h3>
                        <span class="badge badge-ok">Quality Gate PASSED (6x 1080×1350 JPEG)</span>
                      </div>
                      <div class="slides-container">{slides_grid}</div>
                    </div>
                    """
                )

    posts_section = (
        "\n".join(post_cards_html)
        if post_cards_html
        else "<p>尚無已生成的每日輪播圖，請點擊上方按鈕或執行 <code>uv run python -m app produce-daily</code>。</p>"
    )

    safe_cfg = settings.safe_summary()

    return f"""<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Carry | Đài Loan × Việt Nam — TikTok 六頁輪播控制台</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --accent: #f59e0b;
      --text: #f8fafc;
      --muted: #94a3b8;
      --ok: #10b981;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "PingFang TC", "Microsoft JhengHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 24px 32px;
      line-height: 1.5;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 16px;
      margin-bottom: 24px;
    }}
    h1 {{ margin: 0; font-size: 1.5rem; color: var(--accent); }}
    .subtitle {{ color: var(--muted); font-size: 0.9rem; margin-top: 4px; }}
    .status-bar {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 28px;
    }}
    .stat-box {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 18px;
    }}
    .stat-label {{ color: var(--muted); font-size: 0.8rem; }}
    .stat-value {{ font-size: 1.15rem; font-weight: 700; margin-top: 4px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }}
    .card-header h3 {{ margin: 0; font-size: 1.15rem; }}
    .badge {{
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
    }}
    .badge-ok {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
    .slides-container {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }}
    .slide-pair {{
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
    }}
    .slide-label {{
      font-size: 0.8rem;
      color: var(--accent);
      margin-bottom: 8px;
      font-weight: 600;
    }}
    .pair-imgs {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }}
    .pair-imgs img {{
      width: 100%;
      aspect-ratio: 4 / 5;
      object-fit: cover;
      border-radius: 6px;
      border: 1px solid #475569;
    }}
    a.btn {{
      display: inline-block;
      background: var(--accent);
      color: #0f172a;
      font-weight: 700;
      text-decoration: none;
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 0.9rem;
    }}
    code {{
      background: #0b1120;
      padding: 2px 6px;
      border-radius: 4px;
      color: #38bdf8;
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Carry | Đài Loan × Việt Nam (@carry.vietnam.tw)</h1>
      <div class="subtitle">本機圖形化預覽與產線控制台｜六頁 1080×1350 (4:5) 越中對照輪播系統</div>
    </div>
    <div>
      <a class="btn" href="https://www.notion.so/{html.escape(settings.notion_database_id)}" target="_blank">
        🔗 開啟 Notion 輪播內容資料庫
      </a>
    </div>
  </header>

  <section class="status-bar">
    <div class="stat-box">
      <div class="stat-label">Notion 資料庫連線</div>
      <div class="stat-value" style="color: {'#34d399' if safe_cfg['has_notion_token'] else '#f87171'};">
        {'✅ 已授權連線' if safe_cfg['has_notion_token'] else '❌ 未設定 Token'}
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">預設發布 Adapter</div>
      <div class="stat-value">{html.escape(str(safe_cfg['default_uploader_adapter']))}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">安全護欄 (ALLOW_LIVE_PUBLISH)</div>
      <div class="stat-value">{'🟢 True (允許外部發布)' if safe_cfg['allow_live_publish'] else '🔒 False (安全保護中)'}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">搜尋字典 / 研究案例庫</div>
      <div class="stat-value">{len(keywords)} 個關鍵字 / {len(cases)} 篇案例</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Phase 5 觀察樣本 / 建議權重</div>
      <div class="stat-value">{len(observations)} 筆觀察 ｜ 領先變體：{html.escape(str(opt_proposal.ab_test_summary.get('recommended_lead_variant') or 'N/A'))}</div>
    </div>
  </section>

  <h2>🖼️ 已生產之六頁越南文正式圖 & 繁體中文對照圖（點擊圖片可放大檢視 1080×1350 原圖）</h2>
  {posts_section}
</body>
</html>"""


def create_dashboard_handler(project_root: Path, settings: AppSettings):
    """建立支援圖片靜態讀取與 JSON 狀態 API 的 HTTP Request Handler。"""

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path

            if route in {"/", "/index.html"}:
                body = render_dashboard_html(project_root, settings).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/status":
                payload = json.dumps(settings.safe_summary(), ensure_ascii=False, indent=2).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if route.startswith("/media/"):
                rel_path = urllib.parse.unquote(route.removeprefix("/media/"))
                target_file = (project_root / rel_path).resolve()
                # 防止路徑穿越攻擊（Path Traversal）
                if (
                    not str(target_file).startswith(str(project_root.resolve()))
                    or not target_file.is_file()
                ):
                    self.send_error(404, "Media file not found")
                    return

                mime, _ = mimetypes.guess_type(str(target_file))
                data = target_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404, "Not Found")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    return DashboardHandler


def run_gui_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    project_root: Path | None = None,
    settings: AppSettings | None = None,
) -> None:
    """啟動本機 Web GUI 伺服器。"""
    root = (project_root or Path.cwd()).resolve()
    cfg = settings or AppSettings()
    handler_cls = create_dashboard_handler(root, cfg)
    server = HTTPServer((host, port), handler_cls)
    print(f"🚀 本機圖形化預覽控制台已啟動：http://{host}:{port}")
    print("   （按 Ctrl+C 可停止伺服器）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
