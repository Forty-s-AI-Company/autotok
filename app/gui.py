"""本機輕量級圖形預覽與操作介面（Local Web Dashboard GUI）。

遵循 MASTER_SPEC.md 第 10 節：
- 使用 Python 標準庫 `http.server` 提供零額外依賴之本機圖形化控制台（預設 `http://127.0.0.1:8765`）。
- 支援**全圖形化一鍵操作（免背任何終端機指令）**：
  1. 在網頁上選擇日期、時段（全部 / 07:00 / 11:30 / 18:00）、生成引擎（`gpt-6-sol` 寫實生圖或快速測試），一鍵觸發圖片生產與 Notion 同步。
  2. 一鍵執行「越南市場批次研究（Phase 2）」與「成效回饋權重優化報告（Phase 5）」。
  3. 並排預覽每日三時段已生產之六頁越南文正式圖與繁體中文對照圖（點擊可放大檢視 1080×1350 原圖）。
"""

from __future__ import annotations

import html
import json
import mimetypes
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.analytics import AnalyticsAgent, build_mock_observations_fixture
from app.config import AppSettings, redact_sensitive
from app.models import ContentSlot
from app.notion_sync import NotionSyncService
from app.production import (
    ChatGPTCodexCliCarouselGenerator,
    produce_carousel_with_qa_retry,
)
from app.research import (
    INITIAL_SEED_KEYWORDS,
    MockResearchProvider,
    build_topic_opportunities_from_cases,
)
from app.state import PostStateRepository

# 背景工作狀態（供網頁前端即時輪詢顯示進度，免開終端機盯指令）
_TASK_STATE: dict[str, object] = {
    "running": False,
    "task_name": "",
    "status": "idle",
    "message": "尚無執行中的背景任務。可直接點擊上方按鈕一鍵執行！",
    "updated_at": "",
}
_TASK_LOCK = threading.Lock()


def _update_task_state(
    *, running: bool, task_name: str, status: str, message: str
) -> None:
    with _TASK_LOCK:
        _TASK_STATE["running"] = running
        _TASK_STATE["task_name"] = task_name
        _TASK_STATE["status"] = status
        _TASK_STATE["message"] = redact_sensitive(message)
        _TASK_STATE["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_task_state_snapshot() -> dict[str, object]:
    with _TASK_LOCK:
        return dict(_TASK_STATE)


def _run_sync_notion_job(
    project_root: Path,
    settings: AppSettings,
    target_date: str,
    slot_choice: str,
    engine_choice: str,
) -> None:
    """在背景執行緒中生產輪播圖並同步至 Notion。"""
    slot_map = {
        "07:00": [ContentSlot.MORNING_0700],
        "11:30": [ContentSlot.NOON_1130],
        "18:00": [ContentSlot.EVENING_1800],
        "all": [
            ContentSlot.MORNING_0700,
            ContentSlot.NOON_1130,
            ContentSlot.EVENING_1800,
        ],
    }
    target_slots = slot_map.get(slot_choice, slot_map["all"])
    out_base = project_root / "artifacts" / "daily_production" / target_date
    repo = PostStateRepository(settings.sqlite_db_path)
    codex_gen = ChatGPTCodexCliCarouselGenerator(model=settings.codex_model)
    use_codex = engine_choice == "codex" or (
        engine_choice == "auto" and codex_gen.is_available()
    )
    engine_label = f"ChatGPT CLI ({settings.codex_model})" if use_codex else "快速測試渲染 (Mock)"

    try:
        _update_task_state(
            running=True,
            task_name="生產雙語六頁輪播圖並同步 Notion",
            status="running",
            message=f"正在為 {target_date} ({slot_choice}) 生產圖片並上傳 Notion（引擎：{engine_label}）...",
        )
        sync_service = NotionSyncService(settings=settings) if settings.notion_token.strip() else None
        synced_urls: list[str] = []

        for idx, s in enumerate(target_slots, start=1):
            slot_tag = s.value.split("_")[0].replace(":", "")
            post_id = f"post-{target_date}-{slot_tag}"
            _update_task_state(
                running=True,
                task_name="生產雙語六頁輪播圖並同步 Notion",
                status="running",
                message=f"[{idx}/{len(target_slots)}] 正在生成 {post_id} 的 6 張越南文圖與 6 張中文對照圖（{engine_label}）...",
            )
            pkg, _, zh_dir = produce_carousel_with_qa_retry(
                slot=s,
                post_id=post_id,
                scheduled_date_str=target_date,
                output_base_dir=out_base,
                use_codex_cli=use_codex,
                codex_generator=codex_gen,
            )
            repo.upsert_post(pkg)
            if sync_service is not None:
                _update_task_state(
                    running=True,
                    task_name="生產雙語六頁輪播圖並同步 Notion",
                    status="running",
                    message=f"[{idx}/{len(target_slots)}] 正在將 {post_id} 的 12 張圖片上傳至 Notion 雲端...",
                )
                res = sync_service.sync_post_to_notion(pkg, zh_media_dir=zh_dir)
                synced_urls.append(res.notion_page_url)

        done_msg = f"✅ {target_date} ({slot_choice}) 雙語輪播圖生產與 Notion 同步完成！共處理 {len(target_slots)} 篇貼文。"
        if synced_urls:
            done_msg += f" Notion 頁面：{', '.join(synced_urls)}"
        _update_task_state(
            running=False,
            task_name="生產雙語六頁輪播圖並同步 Notion",
            status="completed",
            message=done_msg,
        )
    except Exception as exc:
        _update_task_state(
            running=False,
            task_name="生產雙語六頁輪播圖並同步 Notion",
            status="failed",
            message=f"❌ 執行失敗：{exc}",
        )


def _run_research_and_analytics_job(project_root: Path, settings: AppSettings) -> None:
    """一鍵執行越南市場關鍵字更新、研究案例評分與 Phase 5 每週權重優化報告。"""
    try:
        _update_task_state(
            running=True,
            task_name="市場研究與成效回饋分析",
            status="running",
            message="正在更新 12 組越南文搜尋關鍵字、35 篇研究案例與 Phase 5 權重優化報告...",
        )
        repo = PostStateRepository(settings.sqlite_db_path)
        repo.upsert_seed_keywords(INITIAL_SEED_KEYWORDS)

        provider = MockResearchProvider()
        cases = provider.collect_cases(INITIAL_SEED_KEYWORDS, target_count=35)
        for c in cases:
            repo.upsert_research_case(c)

        existing_topics, used_urls = repo.get_recent_topics_and_urls(days=90)
        opportunities = build_topic_opportunities_from_cases(
            cases, existing_topics=existing_topics, used_urls=used_urls
        )
        for opp in opportunities:
            repo.upsert_topic_opportunity(opp)

        obs_list = build_mock_observations_fixture()
        for o in obs_list:
            repo.upsert_post_observation(o)

        agent = AnalyticsAgent()
        proposal = agent.generate_weekly_optimization_proposal(obs_list)
        md_report = agent.render_markdown_report(proposal)
        out_md = project_root / "artifacts" / "analytics" / "weekly_optimization_report.md"
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md_report, encoding="utf-8")

        _update_task_state(
            running=False,
            task_name="市場研究與成效回饋分析",
            status="completed",
            message=f"✅ 已更新 {len( INITIAL_SEED_KEYWORDS)} 個關鍵字、{len(cases)} 篇研究案例，並產出最新每週優化報告（{out_md.relative_to(project_root)}）！",
        )
    except Exception as exc:
        _update_task_state(
            running=False,
            task_name="市場研究與成效回饋分析",
            status="failed",
            message=f"❌ 研究與成效分析失敗：{exc}",
        )


def render_dashboard_html(project_root: Path, settings: AppSettings) -> str:
    """渲染本機 Web GUI 儀表板 HTML（支援一鍵按鈕操作，免背指令）。"""
    repo = PostStateRepository(settings.sqlite_db_path)
    keywords = repo.list_seed_keywords()
    cases = repo.list_research_cases(days=90)
    observations = repo.list_post_observations(include_mock=True)
    analytics_agent = AnalyticsAgent()
    opt_proposal = analytics_agent.generate_weekly_optimization_proposal(observations)
    task_snap = get_task_state_snapshot()

    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

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

                has_codex_raw = (post_dir / "raw_codex_png").is_dir()
                badge_text = (
                    f"✨ ChatGPT CLI ({settings.codex_model}) 寫實雙語圖 (1080×1350)"
                    if has_codex_raw
                    else "Quality Gate PASSED (6x 1080×1350 JPEG)"
                )

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
                      <div class="slide-label">Slide {idx:02d} (左：越南文正式圖 / 右：繁中對照圖)</div>
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
                        <span class="badge badge-ok">{html.escape(badge_text)}</span>
                      </div>
                      <div class="slides-container">{slides_grid}</div>
                    </div>
                    """
                )

    posts_section = (
        "\n".join(post_cards_html)
        if post_cards_html
        else "<p>尚無已生成的每日輪播圖，請直接在上方操作區選擇日期並點擊「一鍵生成雙語輪播圖＋同步 Notion」。</p>"
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
      --accent-hover: #d97706;
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
      flex-wrap: wrap;
      gap: 12px;
    }}
    h1 {{ margin: 0; font-size: 1.5rem; color: var(--accent); }}
    .subtitle {{ color: var(--muted); font-size: 0.9rem; margin-top: 4px; }}
    .status-bar {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 24px;
    }}
    .stat-box {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 14px 18px;
    }}
    .stat-label {{ color: var(--muted); font-size: 0.8rem; }}
    .stat-value {{ font-size: 1.08rem; font-weight: 700; margin-top: 4px; }}
    .action-panel {{
      background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
      border: 1px solid var(--accent);
      border-radius: 12px;
      padding: 20px;
      margin-bottom: 24px;
    }}
    .action-panel h2 {{
      margin: 0 0 12px 0;
      font-size: 1.15rem;
      color: var(--accent);
    }}
    .form-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: flex-end;
    }}
    .form-group {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .form-group label {{
      font-size: 0.8rem;
      color: var(--muted);
    }}
    input, select {{
      background: #0b1120;
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 0.92rem;
    }}
    button.btn-primary {{
      background: var(--accent);
      color: #0f172a;
      font-weight: 700;
      border: none;
      padding: 9px 18px;
      border-radius: 8px;
      font-size: 0.92rem;
      cursor: pointer;
    }}
    button.btn-primary:hover {{ background: var(--accent-hover); }}
    button.btn-secondary {{
      background: #334155;
      color: var(--text);
      font-weight: 600;
      border: 1px solid #475569;
      padding: 9px 16px;
      border-radius: 8px;
      font-size: 0.9rem;
      cursor: pointer;
    }}
    button.btn-secondary:hover {{ background: #475569; }}
    .task-banner {{
      margin-top: 14px;
      padding: 12px 16px;
      border-radius: 8px;
      background: #0b1120;
      border: 1px solid var(--border);
      font-size: 0.9rem;
    }}
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
      flex-wrap: wrap;
      gap: 8px;
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
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Carry | Đài Loan × Việt Nam (@carry.vietnam.tw)</h1>
      <div class="subtitle">免背指令一鍵圖形化控制台｜六頁 1080×1350 (4:5) 越中對照輪播系統（預設模型：{html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))}）</div>
    </div>
    <div>
      <a class="btn" href="https://www.notion.so/{html.escape(settings.notion_database_id)}" target="_blank">
        🔗 開啟 Notion 輪播內容資料庫
      </a>
    </div>
  </header>

  <section class="action-panel">
    <h2>⚡ 一鍵操作面板（完全免打指令）</h2>
    <div class="form-row">
      <div class="form-group">
        <label for="targetDate">預定發布日期</label>
        <input type="date" id="targetDate" value="{tomorrow_str}"/>
      </div>
      <div class="form-group">
        <label for="slotChoice">發布時段</label>
        <select id="slotChoice">
          <option value="all">全日三時段 (07:00 + 11:30 + 18:00)</option>
          <option value="07:00">07:00 家庭情緒</option>
          <option value="11:30">11:30 台灣生活</option>
          <option value="18:00">18:00 圖文行銷</option>
        </select>
      </div>
      <div class="form-group">
        <label for="engineChoice">圖像生成引擎</label>
        <select id="engineChoice">
          <option value="codex">ChatGPT CLI 寫實生圖 ({html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))})</option>
          <option value="mock">快速排版測試 (Mock)</option>
        </select>
      </div>
      <button class="btn-primary" onclick="triggerSyncNotion()">🚀 一鍵生成雙語輪播圖 ＋ 同步至 Notion</button>
      <button class="btn-secondary" onclick="triggerResearchAnalytics()">📊 一鍵更新市場研究 ＆ 每週權重優化報告</button>
    </div>
    <div id="taskBanner" class="task-banner">
      <strong>目前狀態：</strong> <span id="taskMessage">{html.escape(str(task_snap['message']))}</span>
    </div>
  </section>

  <section class="status-bar">
    <div class="stat-box">
      <div class="stat-label">Notion 資料庫連線</div>
      <div class="stat-value" style="color: {'#34d399' if safe_cfg['has_notion_token'] else '#f87171'};">
        {'✅ 已授權連線' if safe_cfg['has_notion_token'] else '❌ 未設定 Token'}
      </div>
    </div>
    <div class="stat-box">
      <div class="stat-label">ChatGPT CLI 生圖模型</div>
      <div class="stat-value">{html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))}</div>
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

  <script>
    async function triggerSyncNotion() {{
      const dateVal = document.getElementById('targetDate').value;
      const slotVal = document.getElementById('slotChoice').value;
      const engineVal = document.getElementById('engineChoice').value;
      const res = await fetch('/api/run-sync', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{date: dateVal, slot: slotVal, engine: engineVal}})
      }});
      const data = await res.json();
      document.getElementById('taskMessage').textContent = data.message || '已啟動任務';
    }}

    async function triggerResearchAnalytics() {{
      const res = await fetch('/api/run-research-analytics', {{method: 'POST'}});
      const data = await res.json();
      document.getElementById('taskMessage').textContent = data.message || '已啟動研究與分析任務';
    }}

    let wasRunning = false;
    setInterval(async () => {{
      try {{
        const res = await fetch('/api/task-status');
        const st = await res.json();
        document.getElementById('taskMessage').textContent = st.message;
        if (wasRunning && !st.running && st.status === 'completed') {{
          window.location.reload();
        }}
        wasRunning = st.running;
      }} catch (e) {{}}
    }}, 2500);
  </script>
</body>
</html>"""


def create_dashboard_handler(project_root: Path, settings: AppSettings):
    """建立支援圖片靜態讀取、一鍵觸發任務與 JSON 狀態 API 的 HTTP Request Handler。"""

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

            if route == "/api/task-status":
                payload = json.dumps(
                    get_task_state_snapshot(), ensure_ascii=False, indent=2
                ).encode("utf-8")
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

        def do_POST(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            snap = get_task_state_snapshot()
            if snap.get("running"):
                resp = {"ok": False, "message": "目前已有任務正在執行中，請稍候完成。"}
                body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(409)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/run-sync":
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
                params = json.loads(raw_body)
                target_date = str(params.get("date") or "2026-10-21")
                slot_choice = str(params.get("slot") or "all")
                engine_choice = str(params.get("engine") or "codex")

                t = threading.Thread(
                    target=_run_sync_notion_job,
                    args=(project_root, settings, target_date, slot_choice, engine_choice),
                    daemon=True,
                )
                t.start()
                resp = {
                    "ok": True,
                    "message": f"🚀 已啟動 {target_date} ({slot_choice}) 雙語輪播圖生成與 Notion 同步任務！",
                }
                body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/run-research-analytics":
                t = threading.Thread(
                    target=_run_research_and_analytics_job,
                    args=(project_root, settings),
                    daemon=True,
                )
                t.start()
                resp = {
                    "ok": True,
                    "message": "📊 已啟動市場研究與每週權重優化分析任務！",
                }
                body = json.dumps(resp, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
    open_browser: bool = True,
) -> None:
    """啟動本機 Web GUI 伺服器並自動開啟瀏覽器。"""
    root = (project_root or Path.cwd()).resolve()
    cfg = settings or AppSettings()
    handler_cls = create_dashboard_handler(root, cfg)
    server = HTTPServer((host, port), handler_cls)
    url = f"http://{host}:{port}"
    print(f"🚀 Carry | Đài Loan × Việt Nam 圖形化控制台已啟動：{url}")
    print("   （瀏覽器已自動開啟，按 Ctrl+C 可關閉控制台）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
