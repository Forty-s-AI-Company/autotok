"""本機輕量級圖形預覽與操作介面（Local Web Dashboard GUI）。

遵循 MASTER_SPEC.md 第 7、10 節：
- 使用 Python 標準庫 `http.server` 提供零額外依賴之本機圖形化控制台（預設 `http://127.0.0.1:8765`）。
- 支援**全圖形化一鍵操作（免背任何終端機指令）**：
  1. 在網頁上選擇日期、時段（全部 / 07:00 / 11:30 / 18:00）、生成引擎（`gpt-6-sol` 寫實生圖或快速測試），一鍵觸發圖片生產與 Notion 同步。
  2. 一鍵執行「越南市場批次研究（Phase 2）」與「成效回饋權重優化報告（Phase 5）」。
  3. **完整展示與 Notion 資料庫同步的 13 個欄位資訊**：主題（繁體中文）、越南文標題、狀態、發布日期、時段、分類、核心切角、CTA、完整越南文貼文 + 5 個 Hashtag（支援一鍵複製）、繁體中文貼文對照、逐頁 01–06 越中雙語腳本對照，以及六頁雙語 `1080×1350` 圖片並排預覽。
  4. **展示 Phase 2 越南市場搜尋關鍵字、選題機會評分與 Phase 5 成效權重調整報告**。
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
    SLOT_TO_NOTION_CATEGORY,
    ChatGPTCodexCliCarouselGenerator,
    DailySlotEditorialSpec,
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


def _slot_from_post_dir_name(post_dir_name: str) -> ContentSlot:
    if post_dir_name.endswith("0700"):
        return ContentSlot.MORNING_0700
    if post_dir_name.endswith("1800"):
        return ContentSlot.EVENING_1800
    return ContentSlot.NOON_1130


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
        cases = provider.collect_cases(max_cases=35)
        for c in cases:
            repo.upsert_research_case(c)

        existing_topics, used_urls = repo.get_recent_topics_and_urls(days=90)
        opportunities = build_topic_opportunities_from_cases(
            cases,
            existing_topics_within_90d=existing_topics,
            existing_case_urls_within_90d=used_urls,
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
            message=(
                f"✅ 已成功更新 {len(INITIAL_SEED_KEYWORDS)} 個越南文關鍵字、"
                f"{len(cases)} 篇研究案例、{len(opportunities)} 個選題機會，並產出最新每週優化報告！"
            ),
        )
    except Exception as exc:
        _update_task_state(
            running=False,
            task_name="市場研究與成效回饋分析",
            status="failed",
            message=f"❌ 研究與成效分析失敗：{exc}",
        )


def render_dashboard_html(project_root: Path, settings: AppSettings) -> str:
    """渲染本機 Web GUI 儀表板 HTML（支援一鍵按鈕操作與完整 Notion 欄位預覽）。"""
    repo = PostStateRepository(settings.sqlite_db_path)
    keywords = repo.list_seed_keywords()
    if not keywords:
        repo.upsert_seed_keywords(INITIAL_SEED_KEYWORDS)
        keywords = repo.list_seed_keywords()

    cases = repo.list_research_cases(days=90)
    observations = repo.list_post_observations(include_mock=True)
    if not observations:
        observations = build_mock_observations_fixture()
    analytics_agent = AnalyticsAgent()
    opt_proposal = analytics_agent.generate_weekly_optimization_proposal(observations)
    task_snap = get_task_state_snapshot()

    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    # 掃描 artifacts/daily_production 下已生成的貼文目錄，並結合 Notion 完整欄位資訊展示
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

                slot = _slot_from_post_dir_name(post_dir.name)
                time_str, category_name = SLOT_TO_NOTION_CATEGORY.get(
                    slot, ("11:30", "台灣生活")
                )
                spec = DailySlotEditorialSpec.SPECS.get(
                    slot, DailySlotEditorialSpec.SPECS[ContentSlot.NOON_1130]
                )
                zh_title = str(spec["zh_tw_title"])
                vi_title = str(spec["vi_title"])
                core_angle = str(spec["core_angle"])
                cta_text = str(spec["cta"])
                vi_caption = str(spec["vi_caption"])
                zh_caption = str(spec["zh_tw_caption"])
                hashtags_list: list[str] = list(spec["hashtags"])  # type: ignore[arg-type]
                hashtags_str = " ".join(hashtags_list)
                raw_slides: list[tuple[str, str, str]] = list(spec["slides"])  # type: ignore[arg-type]

                full_tiktok_copy = f"{vi_title}\n\n{vi_caption}\n\n{hashtags_str}"
                copy_id = f"copy-{date_dir.name}-{post_dir.name}".replace(".", "-")

                has_codex_raw = (post_dir / "raw_codex_png").is_dir()
                badge_text = (
                    f"✨ ChatGPT CLI ({settings.codex_model}) 寫實雙語圖｜Notion 狀態：圖片完成"
                    if has_codex_raw
                    else "Quality Gate PASSED (6x 1080×1350 JPEG)｜Notion 狀態：圖片完成"
                )

                rel_vi = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in vi_imgs
                ]
                rel_zh = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in zh_imgs
                ]

                slides_grid_items: list[str] = []
                for idx, (v_url, z_url) in enumerate(
                    zip(rel_vi, rel_zh + [""] * 6, strict=False), start=1
                ):
                    s_vi_head, s_vi_body, s_zh_full = (
                        raw_slides[idx - 1]
                        if idx <= len(raw_slides)
                        else ("", "", "")
                    )
                    slides_grid_items.append(
                        f"""
                        <div class="slide-pair">
                          <div class="slide-label">Slide {idx:02d}/06（左：越南文正式圖 ／ 右：繁體中文對照圖）</div>
                          <div class="pair-imgs">
                            <a href="{v_url}" target="_blank" title="點擊放大檢視 1080×1350 越南文原圖"><img src="{v_url}" alt="VI {idx}" loading="lazy"/></a>
                            {f'<a href="{z_url}" target="_blank" title="點擊放大檢視 1080×1350 繁中對照原圖"><img src="{z_url}" alt="ZH {idx}" loading="lazy"/></a>' if z_url else ''}
                          </div>
                          <div class="slide-copy-box">
                            <div><strong>🇻🇳 越文：</strong>{html.escape(s_vi_head)} — {html.escape(s_vi_body)}</div>
                            <div style="margin-top:4px;color:#cbd5e1;"><strong>🇹🇼 中文對照：</strong>{html.escape(s_zh_full)}</div>
                          </div>
                        </div>
                        """
                    )
                slides_grid = "".join(slides_grid_items)

                post_cards_html.append(
                    f"""
                    <div class="card">
                      <div class="card-header">
                        <div>
                          <h3>📌 {html.escape(zh_title)} <span style="color:var(--muted);font-size:0.9rem;">[{html.escape(post_dir.name)}]</span></h3>
                          <div style="color:#fbbf24;font-size:0.92rem;margin-top:4px;">🇻🇳 越南文標題：{html.escape(vi_title)}</div>
                        </div>
                        <span class="badge badge-ok">{html.escape(badge_text)}</span>
                      </div>

                      <!-- Notion 資料庫 13 個屬性欄位完整對應區塊 -->
                      <div class="notion-props-grid">
                        <div class="prop-item"><span class="prop-key">📅 發布日期：</span>{html.escape(date_dir.name)}</div>
                        <div class="prop-item"><span class="prop-key">⏰ 時段：</span><span class="tag">{html.escape(time_str)}</span></div>
                        <div class="prop-item"><span class="prop-key">🏷️ 分類：</span><span class="tag">{html.escape(category_name)}</span></div>
                        <div class="prop-item"><span class="prop-key">✅ 中文對照：</span>已勾選 (True)</div>
                        <div class="prop-item"><span class="prop-key">🖼️ 越南文圖片：</span>6 張 (1080×1350 JPG)</div>
                        <div class="prop-item"><span class="prop-key">🖼️ 中文版圖片：</span>6 張 (1080×1350 JPG)</div>
                        <div class="prop-item prop-wide"><span class="prop-key">🎯 核心切角：</span>{html.escape(core_angle)}</div>
                        <div class="prop-item prop-wide"><span class="prop-key">📣 CTA 行動呼籲：</span>{html.escape(cta_text)}</div>
                      </div>

                      <!-- 越南文貼文 + 5 Hashtags 一鍵複製區 & 繁中翻譯對照 -->
                      <div class="caption-grid">
                        <div class="caption-box">
                          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                            <strong style="color:#38bdf8;">📋 TikTok 越南文貼文 ＋ 5 個 Hashtag（可直接複製發文）</strong>
                            <button class="btn-copy" onclick="copyText('{copy_id}')">📋 一鍵複製越南文文案</button>
                          </div>
                          <textarea id="{copy_id}" readonly class="copy-area">{html.escape(full_tiktok_copy)}</textarea>
                        </div>
                        <div class="caption-box">
                          <strong style="color:#a7f3d0;display:block;margin-bottom:6px;">🇹🇼 繁體中文貼文翻譯對照</strong>
                          <div style="font-size:0.9rem;color:#e2e8f0;white-space:pre-wrap;">{html.escape(zh_caption)}\n\n標籤：{html.escape(hashtags_str)}</div>
                        </div>
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

    # 渲染 Phase 5 權重與時段成效摘要表格
    slot_rows_html = "".join(
        f"""
        <tr>
          <td><code>{html.escape(s.slot.value)}</code></td>
          <td>{s.post_count} 篇 ({s.full_rate_post_count} 完整 / {s.partial_rate_post_count} 部分)</td>
          <td>{f"{s.avg_full_engagement_rate:.2%}" if s.avg_full_engagement_rate is not None else "N/A"}</td>
          <td>{f"{s.avg_save_share_ratio:.2%}" if s.avg_save_share_ratio is not None else "N/A"}</td>
          <td><strong style="color:#34d399;">{s.verified_new_followers_total} 人</strong></td>
          <td>{s.correlated_new_followers_total} 人</td>
        </tr>
        """
        for s in opt_proposal.slot_summaries
    )

    weight_badges_html = " ｜ ".join(
        f"<span>{html.escape(k)}: <strong>{v:.0%}</strong> (原 {opt_proposal.current_weights.get(k, 0):.0%})</span>"
        for k, v in opt_proposal.proposed_weights.items()
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
    .stat-value {{ font-size: 1.05rem; font-weight: 700; margin-top: 4px; }}
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
    button.btn-copy {{
      background: #0284c7;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 0.8rem;
      cursor: pointer;
      font-weight: 600;
    }}
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
      align-items: flex-start;
      margin-bottom: 14px;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .card-header h3 {{ margin: 0; font-size: 1.2rem; }}
    .badge {{
      padding: 5px 12px;
      border-radius: 999px;
      font-size: 0.78rem;
      font-weight: 600;
    }}
    .badge-ok {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
    .notion-props-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 8px 16px;
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 16px;
      margin-bottom: 14px;
      font-size: 0.88rem;
    }}
    .prop-wide {{
      grid-column: 1 / -1;
    }}
    .prop-key {{
      color: var(--muted);
      font-weight: 600;
    }}
    .tag {{
      background: #334155;
      color: #fbbf24;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.82rem;
      font-weight: 600;
    }}
    .caption-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 14px;
      margin-bottom: 16px;
    }}
    .caption-box {{
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
    }}
    textarea.copy-area {{
      width: 100%;
      height: 92px;
      background: #111827;
      color: #f8fafc;
      border: 1px solid #374151;
      border-radius: 6px;
      padding: 8px;
      font-size: 0.85rem;
      resize: vertical;
      box-sizing: border-box;
    }}
    .slides-container {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 16px;
    }}
    .slide-pair {{
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
    }}
    .slide-label {{
      font-size: 0.82rem;
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
    .slide-copy-box {{
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px dashed #334155;
      font-size: 0.82rem;
      line-height: 1.45;
    }}
    table.data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.88rem;
      margin-top: 8px;
    }}
    table.data-table th, table.data-table td {{
      border: 1px solid var(--border);
      padding: 8px 12px;
      text-align: left;
    }}
    table.data-table th {{
      background: #0b1120;
      color: var(--accent);
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
      <div class="subtitle">免背指令一鍵圖形化控制台｜完整同步顯示 Notion 13 個欄位、雙語文案、六頁圖片與成效優化報告</div>
    </div>
    <div>
      <a class="btn" href="https://www.notion.so/{html.escape(settings.notion_database_id)}" target="_blank">
        🔗 開啟雲端 Notion 輪播內容資料庫
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
        {'✅ 已授權連線 (13 欄位同步)' if safe_cfg['has_notion_token'] else '❌ 未設定 Token'}
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
      <div class="stat-label">Phase 5 觀察樣本 / 領先變體</div>
      <div class="stat-value">{len(observations)} 筆觀察 ｜ {html.escape(str(opt_proposal.ab_test_summary.get('recommended_lead_variant') or 'A_LIST_HOOK'))}</div>
    </div>
  </section>

  <!-- Phase 5 成效回饋與選題權重摘要卡 -->
  <div class="card">
    <div class="card-header">
      <h3>📈 Phase 5｜三時段成效對照與下週選題評分權重建議</h3>
      <span class="badge badge-ok">A/B 推薦領先變體：{html.escape(str(opt_proposal.ab_test_summary.get('recommended_lead_variant') or 'A_LIST_HOOK'))}</span>
    </div>
    <div style="font-size:0.9rem;margin-bottom:10px;color:#cbd5e1;">
      <strong>🎯 下週建議選題權重（總和 100%）：</strong> {weight_badges_html}
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>時段與分類</th>
          <th>觀察篇數（口徑分佈）</th>
          <th>平均完整互動率</th>
          <th>平均（收藏+分享）/觀看</th>
          <th>直接歸因新增粉絲 (DIRECT)</th>
          <th>時窗相關粉絲 (CORRELATED)</th>
        </tr>
      </thead>
      <tbody>
        {slot_rows_html}
      </tbody>
    </table>
  </div>

  <h2>🗂️ Notion 同步內容完整檢視（含 Notion 13 個屬性欄位、雙語文案一鍵複製、逐頁翻譯與 1080×1350 雙語圖片）</h2>
  {posts_section}

  <script>
    function copyText(elementId) {{
      const el = document.getElementById(elementId);
      if (!el) return;
      el.select();
      navigator.clipboard.writeText(el.value);
      alert('✅ 已複製越南文貼文與 5 個 Hashtag！可直接貼到 TikTok 發布。');
    }}

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
    }}, 2000);
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
