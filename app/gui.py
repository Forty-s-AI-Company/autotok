"""本機現代化側邊欄圖形控制台（Modern Sidebar Web Dashboard GUI）。

針對台灣創作者與多帳號營運需求全新設計：
1. **現代 SaaS 側邊欄儀表板 (Sidebar Dashboard)**：
   - 柔和深色毛玻璃與細緻微邊框視覺設計，告別生硬粗框線。
   - 左側導覽列整合「多帳號切換器」、「內容與 Notion 工作台」、「智慧內容切分生圖」、「市場研究與數據分析」、「帳號管理設定」四大分頁。
2. **多 TikTok 帳號管理與切換 (Multi-Account Switcher)**：
   - 支援單一使用者管理多個 TikTok 帳號（如 `@carry.vietnam.tw`、`@carry.tw.food` 等），可一鍵切換或新增帳號。
3. **台灣使用者友善：繁體中文圖片預設主顯示、外語圖片預設收合省空間**：
   - 貼文卡片預設直接展示 **🇹🇼 繁體中文版圖片 (`zh_tw`)**，方便台灣創作者一眼審閱內容。
   - **🇻🇳 越南文正式圖片 (`vi`)** 預設收合隱藏，點擊「展開越南文圖片與越文腳本」才打開，大幅節省畫面空間。
4. **動態圖片張數（依內容切分 3–10 張，不再死板固定 6 張）**：
   - 提供「智慧內容切分器」，輸入想分享的主題或條列重點後，系統自動依內容切分並決定最適張數（封面 + N 個重點 + 尾頁 CTA，支援 3–10 張），亦可即時預覽切分結果。
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from app.analytics import AnalyticsAgent, build_mock_observations_fixture
from app.config import AppSettings, redact_sensitive
from app.models import ContentSlot, TikTokAccountProfile
from app.notion_sync import NotionSyncService
from app.production import (
    SLOT_TO_NOTION_CATEGORY,
    ChatGPTCodexCliCarouselGenerator,
    DailySlotEditorialSpec,
    produce_carousel_with_qa_retry,
    split_content_into_slides,
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
    "message": "系統待命中 — 可直接於上方選擇日期或輸入內容一鍵生成！",
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
    if "0700" in post_dir_name:
        return ContentSlot.MORNING_0700
    if "1800" in post_dir_name:
        return ContentSlot.EVENING_1800
    return ContentSlot.NOON_1130


def _run_sync_notion_job(
    project_root: Path,
    settings: AppSettings,
    target_date: str,
    slot_choice: str,
    engine_choice: str,
    account_id: str = "carry-vietnam-tw",
    custom_content: str = "",
    slide_count_choice: str = "auto",
) -> None:
    """在背景執行緒中依內容切分決定張數、生產雙語輪播圖並同步至 Notion。"""
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
    active_acc = repo.set_active_account(account_id) if account_id else repo.get_active_account()

    target_slide_count: int | None = None
    if slide_count_choice and slide_count_choice != "auto":
        try:
            target_slide_count = int(slide_count_choice)
        except ValueError:
            target_slide_count = None

    codex_gen = ChatGPTCodexCliCarouselGenerator(model=settings.codex_model)
    use_codex = engine_choice == "codex" or (
        engine_choice == "auto" and codex_gen.is_available()
    )
    engine_label = (
        f"ChatGPT CLI ({settings.codex_model})" if use_codex else "快速測試渲染 (Mock)"
    )

    try:
        _update_task_state(
            running=True,
            task_name="生產雙語輪播圖並同步 Notion",
            status="running",
            message=f"正在為帳號 {active_acc.handle} 於 {target_date} ({slot_choice}) 切分內容並生成圖片（{engine_label}）...",
        )
        sync_service = (
            NotionSyncService(settings=settings) if settings.notion_token.strip() else None
        )
        synced_urls: list[str] = []

        for idx, s in enumerate(target_slots, start=1):
            slot_tag = s.value.split("_")[0].replace(":", "")
            post_id = (
                f"post-{target_date}-{slot_tag}"
                if active_acc.account_id == "carry-vietnam-tw"
                else f"post-{active_acc.account_id}-{target_date}-{slot_tag}"
            )
            preview_slides = split_content_into_slides(
                custom_content,
                slot=s,
                account_handle=active_acc.handle,
                target_slide_count=target_slide_count,
            )
            n_slides = len(preview_slides)
            _update_task_state(
                running=True,
                task_name="生產雙語輪播圖並同步 Notion",
                status="running",
                message=f"[{idx}/{len(target_slots)}] 正在為 {post_id} 生成 {n_slides} 張繁中主圖 + {n_slides} 張越文圖（{engine_label}）...",
            )
            pkg, _, zh_dir = produce_carousel_with_qa_retry(
                slot=s,
                post_id=post_id,
                scheduled_date_str=target_date,
                output_base_dir=out_base,
                account_id=active_acc.account_id,
                account_handle=active_acc.handle,
                custom_content=custom_content,
                target_slide_count=target_slide_count,
                use_codex_cli=use_codex,
                codex_generator=codex_gen,
            )
            repo.upsert_post(pkg)
            if sync_service is not None:
                _update_task_state(
                    running=True,
                    task_name="生產雙語輪播圖並同步 Notion",
                    status="running",
                    message=f"[{idx}/{len(target_slots)}] 正在將 {post_id}（共 {pkg.slide_count} 頁雙語圖）上傳至 Notion 雲端...",
                )
                res = sync_service.sync_post_to_notion(pkg, zh_media_dir=zh_dir)
                synced_urls.append(res.notion_page_url)

        done_msg = (
            f"✅ 帳號 {active_acc.handle} 於 {target_date} ({slot_choice}) 輪播圖生產與 Notion 同步完成！"
            f"共完成 {len(target_slots)} 篇貼文。"
        )
        if synced_urls:
            done_msg += f" Notion 頁面：{', '.join(synced_urls)}"
        _update_task_state(
            running=False,
            task_name="生產雙語輪播圖並同步 Notion",
            status="completed",
            message=done_msg,
        )
    except Exception as exc:
        _update_task_state(
            running=False,
            task_name="生產雙語輪播圖並同步 Notion",
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


def render_dashboard_html(
    project_root: Path,
    settings: AppSettings,
    *,
    selected_account_id: str | None = None,
) -> str:
    """渲染現代化側邊欄 Web Dashboard（支援多帳號切換、中文圖主顯示＋外語圖預設收合、動態張數切分）。"""
    repo = PostStateRepository(settings.sqlite_db_path)
    if selected_account_id:
        active_account = repo.set_active_account(selected_account_id)
    else:
        active_account = repo.get_active_account()
    all_accounts = repo.list_accounts()

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

    # 讀取已儲存在 SQLite 的自訂貼文 metadata（若有）以支援自訂切分文案與張數顯示
    saved_posts_by_id = {p.post_id: p for p in repo.list_posts()}

    # 掃描 artifacts/daily_production 下已生成的貼文目錄
    prod_root = project_root / "artifacts" / "daily_production"
    post_cards_html: list[str] = []
    total_rendered_posts = 0

    if prod_root.is_dir():
        for date_dir in sorted(prod_root.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            for post_dir in sorted(date_dir.iterdir()):
                if not post_dir.is_dir():
                    continue
                vi_dir = post_dir / "vi"
                zh_dir = post_dir / "zh_tw"
                vi_imgs = sorted(vi_dir.glob("slide_*.jpg")) if vi_dir.is_dir() else []
                zh_imgs = sorted(zh_dir.glob("slide_*.jpg")) if zh_dir.is_dir() else []
                if not vi_imgs and not zh_imgs:
                    continue

                saved_pkg = saved_posts_by_id.get(post_dir.name)
                post_account_id = (
                    saved_pkg.account_id
                    if saved_pkg is not None
                    else (
                        "carry-vietnam-tw"
                        if not post_dir.name.startswith("post-carry-")
                        else post_dir.name.split("-202")[0].removeprefix("post-")
                    )
                )
                # 若使用者切換了特定帳號，優先顯示該帳號貼文（若該帳號尚無專屬貼文則顯示主帳號範例並標示）
                slot = saved_pkg.slot if saved_pkg else _slot_from_post_dir_name(post_dir.name)
                time_str, category_name = SLOT_TO_NOTION_CATEGORY.get(
                    slot, ("11:30", "台灣生活")
                )
                spec = DailySlotEditorialSpec.SPECS.get(
                    slot, DailySlotEditorialSpec.SPECS[ContentSlot.NOON_1130]
                )

                zh_title = saved_pkg.zh_tw_title if saved_pkg else str(spec["zh_tw_title"])
                vi_title = saved_pkg.vi_title if saved_pkg else str(spec["vi_title"])
                core_angle = (
                    saved_pkg.core_angle
                    if (saved_pkg and saved_pkg.core_angle)
                    else str(spec["core_angle"])
                )
                cta_text = (
                    saved_pkg.cta if (saved_pkg and saved_pkg.cta) else str(spec["cta"])
                )
                vi_caption = saved_pkg.vi_caption if saved_pkg else str(spec["vi_caption"])
                zh_caption = (
                    saved_pkg.zh_tw_caption if saved_pkg else str(spec["zh_tw_caption"])
                )
                hashtags_list: list[str] = (
                    list(saved_pkg.hashtags)
                    if saved_pkg
                    else list(spec["hashtags"])  # type: ignore[arg-type]
                )
                hashtags_str = " ".join(hashtags_list)

                primary_imgs = zh_imgs if zh_imgs else vi_imgs
                slide_total = len(primary_imgs)

                if saved_pkg and len(saved_pkg.slides) == slide_total:
                    slide_texts = [
                        (s.vi_text, s.zh_tw_translation) for s in saved_pkg.slides
                    ]
                else:
                    raw_spec_slides: list[tuple[str, str, str]] = list(spec["slides"])  # type: ignore[arg-type]
                    slide_texts = [
                        (f"{vh} — {vb}", zf) for vh, vb, zf in raw_spec_slides
                    ]

                full_tiktok_copy = f"{vi_title}\n\n{vi_caption}\n\n{hashtags_str}"
                copy_id = f"copy-{date_dir.name}-{post_dir.name}".replace(".", "-")
                vi_drawer_id = f"vi-drawer-{date_dir.name}-{post_dir.name}".replace(".", "-")

                has_codex_raw = (post_dir / "raw_codex_png").is_dir()
                engine_badge = (
                    f"✨ ChatGPT CLI ({settings.codex_model})"
                    if has_codex_raw
                    else "⚡ 快速視覺渲染"
                )

                rel_zh = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in primary_imgs
                ]
                rel_vi = [
                    "/media/" + str(p.relative_to(project_root)).replace("\\", "/")
                    for p in vi_imgs
                ]

                # 1. 台灣使用者主視圖：預設直接顯示繁體中文圖片（zh_tw），排版清爽不擁擠
                zh_slides_html_items: list[str] = []
                for idx, z_url in enumerate(rel_zh, start=1):
                    _, s_zh = (
                        slide_texts[idx - 1]
                        if idx <= len(slide_texts)
                        else ("", f"第 {idx} 頁重點")
                    )
                    zh_slides_html_items.append(
                        f"""
                        <div class="slide-card">
                          <div class="slide-card-top">
                            <span class="slide-num">第 {idx:02d} / {slide_total:02d} 頁</span>
                            <span class="lang-pill lang-zh">🇹🇼 繁體中文圖</span>
                          </div>
                          <a href="{z_url}" target="_blank" class="slide-img-link" title="點擊放大檢視 1080×1350 繁體中文原圖">
                            <img src="{z_url}" alt="中文圖 {idx}" loading="lazy"/>
                          </a>
                          <div class="slide-caption-zh">{html.escape(s_zh)}</div>
                        </div>
                        """
                    )
                zh_slides_grid = "".join(zh_slides_html_items)

                # 2. 其他語系（越南文正式發布圖）：預設收合隱藏，需要檢查外語排版時再點開，大幅節省空間
                vi_slides_html_items: list[str] = []
                for idx, v_url in enumerate(rel_vi, start=1):
                    s_vi, _ = (
                        slide_texts[idx - 1]
                        if idx <= len(slide_texts)
                        else (f"Trang {idx}", "")
                    )
                    vi_slides_html_items.append(
                        f"""
                        <div class="slide-card slide-card-vi">
                          <div class="slide-card-top">
                            <span class="slide-num">Slide {idx:02d} / {slide_total:02d}</span>
                            <span class="lang-pill lang-vi">🇻🇳 越南文發布圖</span>
                          </div>
                          <a href="{v_url}" target="_blank" class="slide-img-link" title="點擊放大檢視 1080×1350 越南文原圖">
                            <img src="{v_url}" alt="越文圖 {idx}" loading="lazy"/>
                          </a>
                          <div class="slide-caption-vi">{html.escape(s_vi)}</div>
                        </div>
                        """
                    )
                vi_slides_grid = "".join(vi_slides_html_items)

                total_rendered_posts += 1
                post_cards_html.append(
                    f"""
                    <article class="post-card" data-account="{html.escape(post_account_id)}">
                      <div class="post-card-header">
                        <div class="post-title-group">
                          <div class="post-meta-row">
                            <span class="pill pill-slot">⏰ {html.escape(date_dir.name)} · {html.escape(time_str)}</span>
                            <span class="pill pill-cat">🏷️ {html.escape(category_name)}</span>
                            <span class="pill pill-count">📑 依內容切分：共 {slide_total} 張圖</span>
                            <span class="pill pill-engine">{html.escape(engine_badge)}</span>
                            <span class="pill pill-status">✅ Notion 狀態：圖片完成</span>
                          </div>
                          <h3 class="post-zh-title">🇹🇼 {html.escape(zh_title)} <span class="post-id-label">({html.escape(post_dir.name)})</span></h3>
                          <div class="post-vi-sub">🇻🇳 越南文標題：{html.escape(vi_title)}</div>
                        </div>
                      </div>

                      <!-- 精簡優雅的 Notion 同步屬性列 -->
                      <div class="notion-meta-bar">
                        <div class="meta-cell"><span class="meta-label">🎯 核心切角</span><span class="meta-val">{html.escape(core_angle)}</span></div>
                        <div class="meta-cell"><span class="meta-label">📣 尾頁 CTA</span><span class="meta-val">{html.escape(cta_text)}</span></div>
                      </div>

                      <!-- 🇹🇼 台灣創作者主視圖：繁體中文圖片橫向網格（預設直接顯示） -->
                      <div class="section-bar">
                        <div class="section-bar-title">
                          <strong>🇹🇼 繁體中文主視圖（共 {slide_total} 張 · 1080×1350）</strong>
                          <span class="muted-hint">預設顯示中文圖方便審閱內容；點擊圖片可放大檢視原圖</span>
                        </div>
                        <button type="button" class="btn-toggle-vi" onclick="toggleForeignDrawer('{vi_drawer_id}', this)">
                          👁️ 展開越南文圖片與外語貼文（共 {len(rel_vi)} 張 · 預設收合省空間）
                        </button>
                      </div>
                      <div class="slides-grid">{zh_slides_grid}</div>

                      <!-- 🇻🇳 外語（越南文）圖片與越文複製區：預設收合隱藏，需要時一鍵打開 -->
                      <div id="{vi_drawer_id}" class="foreign-drawer" style="display:none;">
                        <div class="foreign-drawer-header">
                          <strong>🇻🇳 越南文正式發布圖片（共 {len(rel_vi)} 張）＆ TikTok 越南文發文文案</strong>
                          <button type="button" class="btn-copy" onclick="copyText('{copy_id}')">📋 一鍵複製越南文貼文 + 5 Hashtags</button>
                        </div>
                        <div class="caption-split">
                          <div>
                            <label class="small-label">🇻🇳 TikTok 越南文貼文 ＋ 5 個 Hashtag（可直接複製貼上）</label>
                            <textarea id="{copy_id}" readonly class="copy-area">{html.escape(full_tiktok_copy)}</textarea>
                          </div>
                          <div>
                            <label class="small-label">🇹🇼 繁體中文貼文翻譯對照</label>
                            <div class="zh-caption-box">{html.escape(zh_caption)}\n\n<span style="color:#94a3b8;">Hashtags：{html.escape(hashtags_str)}</span></div>
                          </div>
                        </div>
                        <div class="slides-grid" style="margin-top:14px;">{vi_slides_grid}</div>
                      </div>
                    </article>
                    """
                )

    posts_section = (
        "\n".join(post_cards_html)
        if post_cards_html
        else '<div class="empty-state">尚無已生成的輪播貼文，請於上方操作區或左側「智慧內容切分生圖」一鍵產出！</div>'
    )

    # 側邊欄多帳號下拉選項與帳號管理卡片
    account_options_html = "".join(
        f'<option value="{html.escape(acc.account_id)}" {"selected" if acc.account_id == active_account.account_id else ""}>'
        f"{html.escape(acc.handle)} — {html.escape(acc.display_name)}</option>"
        for acc in all_accounts
    )

    accounts_table_rows = "".join(
        f"""
        <tr>
          <td><strong>{html.escape(acc.handle)}</strong></td>
          <td>{html.escape(acc.display_name)}</td>
          <td>{html.escape(acc.market_focus)}</td>
          <td><span class="pill pill-status">主視圖：{html.escape(acc.primary_language)} ／ 發布：{html.escape(acc.target_language)}</span></td>
          <td><code>{html.escape(acc.notion_database_id[:16])}...</code></td>
          <td>
            {"<span class='pill pill-engine'>🟢 目前使用中</span>" if acc.account_id == active_account.account_id else f"<button class='btn-ghost' onclick=\"switchAccount('{html.escape(acc.account_id)}')\">切換至此帳號</button>"}
          </td>
        </tr>
        """
        for acc in all_accounts
    )

    # Phase 5 權重與時段成效摘要表格
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

    weight_badges_html = " ".join(
        f'<span class="pill pill-slot">{html.escape(k)}: <strong>{v:.0%}</strong> (原 {opt_proposal.current_weights.get(k, 0):.0%})</span>'
        for k, v in opt_proposal.proposed_weights.items()
    )

    keywords_rows_html = "".join(
        f"""
        <tr>
          <td><strong>{html.escape(kw.keyword_vi)}</strong></td>
          <td>{html.escape(kw.translation_zh_tw)}</td>
          <td>{html.escape(kw.intent)}</td>
          <td><code>{html.escape(kw.recommended_slot.value)}</code></td>
        </tr>
        """
        for kw in keywords[:12]
    )

    safe_cfg = settings.safe_summary()

    return f"""<!DOCTYPE html>
<html lang="zh-Hant-TW">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{html.escape(active_account.display_name)} — TikTok 多帳號智慧輪播工作台</title>
  <style>
    :root {{
      --bg-main: #090d16;
      --bg-sidebar: #0f1624;
      --bg-surface: #151e31;
      --bg-elevated: #1c2840;
      --border-subtle: rgba(148, 163, 184, 0.14);
      --border-focus: rgba(99, 102, 241, 0.55);
      --primary: #6366f1;
      --primary-hover: #4f46e5;
      --amber: #f59e0b;
      --emerald: #10b981;
      --text-main: #f1f5f9;
      --text-secondary: #cbd5e1;
      --text-muted: #94a3b8;
      --radius-lg: 14px;
      --radius-md: 10px;
      --radius-sm: 8px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
      background: var(--bg-main);
      color: var(--text-main);
      margin: 0;
      display: flex;
      min-height: 100vh;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }}

    /* 左側固定側邊欄 (Sidebar) */
    .sidebar {{
      width: 280px;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border-subtle);
      display: flex;
      flex-direction: column;
      padding: 22px 18px;
      position: sticky;
      top: 0;
      height: 100vh;
      flex-shrink: 0;
    }}
    .brand-box {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--border-subtle);
      margin-bottom: 18px;
    }}
    .brand-avatar {{
      width: 40px;
      height: 40px;
      border-radius: 10px;
      background: linear-gradient(135deg, #6366f1 0%, #f59e0b 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 1.15rem;
      color: #fff;
      flex-shrink: 0;
    }}
    .brand-title {{
      font-size: 0.96rem;
      font-weight: 700;
      color: var(--text-main);
      line-height: 1.25;
    }}
    .brand-sub {{
      font-size: 0.76rem;
      color: var(--text-muted);
      margin-top: 2px;
    }}

    /* 多帳號切換區塊 */
    .account-switcher {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 12px;
      margin-bottom: 20px;
    }}
    .account-switcher-label {{
      font-size: 0.74rem;
      color: var(--text-muted);
      font-weight: 600;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }}
    .account-select {{
      width: 100%;
      background: var(--bg-main);
      color: var(--text-main);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 8px 10px;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .account-focus-note {{
      font-size: 0.75rem;
      color: var(--text-muted);
      margin-top: 8px;
      line-height: 1.35;
    }}
    .btn-add-account {{
      margin-top: 8px;
      width: 100%;
      background: rgba(99, 102, 241, 0.14);
      color: #a5b4fc;
      border: 1px dashed rgba(99, 102, 241, 0.4);
      border-radius: var(--radius-sm);
      padding: 6px 10px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .btn-add-account:hover {{
      background: rgba(99, 102, 241, 0.25);
      color: #fff;
    }}

    /* 側邊欄導覽選單 */
    .nav-menu {{
      display: flex;
      flex-direction: column;
      gap: 6px;
      flex: 1;
    }}
    .nav-btn {{
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      text-align: left;
      background: transparent;
      color: var(--text-secondary);
      border: none;
      border-radius: var(--radius-sm);
      padding: 11px 12px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .nav-btn:hover {{
      background: rgba(255, 255, 255, 0.05);
      color: #fff;
    }}
    .nav-btn.active {{
      background: var(--primary);
      color: #fff;
      box-shadow: 0 4px 14px rgba(99, 102, 241, 0.35);
    }}
    .sidebar-footer {{
      border-top: 1px solid var(--border-subtle);
      padding-top: 14px;
      font-size: 0.78rem;
      color: var(--text-muted);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .notion-link-btn {{
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      background: var(--bg-surface);
      color: var(--text-main);
      text-decoration: none;
      padding: 9px 12px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--border-subtle);
      font-weight: 600;
      font-size: 0.82rem;
      transition: background 0.15s;
    }}
    .notion-link-btn:hover {{
      background: var(--bg-elevated);
    }}

    /* 右側主內容區 */
    .main-area {{
      flex: 1;
      padding: 26px 34px;
      max-width: 1440px;
      overflow-y: auto;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 22px;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .topbar-title {{
      font-size: 1.35rem;
      font-weight: 800;
      margin: 0;
    }}
    .topbar-sub {{
      font-size: 0.85rem;
      color: var(--text-muted);
      margin-top: 2px;
    }}
    .topbar-actions {{
      display: flex;
      gap: 10px;
      align-items: center;
    }}

    /* 任務即時狀態列 */
    .task-toast {{
      background: var(--bg-surface);
      border: 1px solid var(--border-focus);
      border-radius: var(--radius-md);
      padding: 12px 16px;
      margin-bottom: 22px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      font-size: 0.88rem;
    }}
    .kpi-row {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-bottom: 24px;
    }}
    .kpi-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 14px 16px;
    }}
    .kpi-label {{
      font-size: 0.78rem;
      color: var(--text-muted);
    }}
    .kpi-val {{
      font-size: 1.05rem;
      font-weight: 700;
      margin-top: 4px;
    }}

    /* 快速操作與智慧拆頁面板 */
    .studio-panel {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 20px 22px;
      margin-bottom: 24px;
    }}
    .studio-panel h2 {{
      margin: 0 0 6px 0;
      font-size: 1.08rem;
    }}
    .form-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .field-box {{
      display: flex;
      flex-direction: column;
      gap: 5px;
    }}
    .field-box label {{
      font-size: 0.78rem;
      color: var(--text-muted);
      font-weight: 600;
    }}
    input, select, textarea {{
      background: var(--bg-main);
      color: var(--text-main);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 9px 12px;
      font-size: 0.88rem;
      font-family: inherit;
    }}
    input:focus, select:focus, textarea:focus {{
      outline: none;
      border-color: var(--primary);
    }}
    .btn-primary {{
      background: var(--primary);
      color: #fff;
      border: none;
      border-radius: var(--radius-sm);
      padding: 10px 18px;
      font-size: 0.88rem;
      font-weight: 700;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .btn-primary:hover {{ background: var(--primary-hover); }}
    .btn-ghost {{
      background: var(--bg-elevated);
      color: var(--text-secondary);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 9px 14px;
      font-size: 0.84rem;
      font-weight: 600;
      cursor: pointer;
    }}
    .btn-ghost:hover {{ color: #fff; background: #263554; }}

    /* 貼文卡片 (Post Card) — 乾淨無粗框線 */
    .post-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 22px;
      margin-bottom: 22px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.22);
    }}
    .post-meta-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 0.76rem;
      font-weight: 600;
    }}
    .pill-slot {{ background: rgba(99, 102, 241, 0.16); color: #a5b4fc; }}
    .pill-cat {{ background: rgba(245, 158, 11, 0.16); color: #fcd34d; }}
    .pill-count {{ background: rgba(14, 165, 233, 0.16); color: #7dd3fc; }}
    .pill-engine {{ background: rgba(168, 85, 247, 0.16); color: #d8b4fe; }}
    .pill-status {{ background: rgba(16, 185, 129, 0.16); color: #6ee7b7; }}

    .post-zh-title {{
      margin: 0;
      font-size: 1.18rem;
      font-weight: 700;
      color: var(--text-main);
    }}
    .post-id-label {{
      font-size: 0.82rem;
      color: var(--text-muted);
      font-weight: 400;
    }}
    .post-vi-sub {{
      font-size: 0.86rem;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .notion-meta-bar {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 10px 18px;
      background: rgba(9, 13, 22, 0.55);
      border-radius: var(--radius-md);
      padding: 12px 16px;
      margin: 14px 0;
      font-size: 0.84rem;
    }}
    .meta-label {{
      color: var(--text-muted);
      font-weight: 600;
      margin-right: 8px;
    }}
    .meta-val {{ color: var(--text-secondary); }}

    .section-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin: 14px 0 12px 0;
    }}
    .section-bar-title {{
      display: flex;
      align-items: baseline;
      gap: 10px;
      flex-wrap: wrap;
      font-size: 0.92rem;
    }}
    .muted-hint {{
      font-size: 0.78rem;
      color: var(--text-muted);
    }}
    .btn-toggle-vi {{
      background: rgba(245, 158, 11, 0.14);
      color: #fcd34d;
      border: 1px solid rgba(245, 158, 11, 0.32);
      border-radius: var(--radius-sm);
      padding: 6px 12px;
      font-size: 0.8rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .btn-toggle-vi:hover {{
      background: rgba(245, 158, 11, 0.25);
    }}

    /* 圖片網格：支援 3–10 張動態自適應 */
    .slides-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(195px, 1fr));
      gap: 14px;
    }}
    .slide-card {{
      background: var(--bg-main);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-md);
      padding: 10px;
      display: flex;
      flex-direction: column;
      transition: transform 0.15s;
    }}
    .slide-card:hover {{
      transform: translateY(-2px);
    }}
    .slide-card-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 0.76rem;
    }}
    .slide-num {{
      font-weight: 700;
      color: var(--text-secondary);
    }}
    .lang-pill {{
      padding: 2px 7px;
      border-radius: 6px;
      font-size: 0.7rem;
      font-weight: 600;
    }}
    .lang-zh {{ background: rgba(16, 185, 129, 0.16); color: #6ee7b7; }}
    .lang-vi {{ background: rgba(245, 158, 11, 0.16); color: #fcd34d; }}
    .slide-img-link img {{
      width: 100%;
      aspect-ratio: 4 / 5;
      object-fit: cover;
      border-radius: 8px;
      display: block;
    }}
    .slide-caption-zh, .slide-caption-vi {{
      margin-top: 8px;
      font-size: 0.78rem;
      color: var(--text-secondary);
      line-height: 1.4;
    }}

    /* 收合式外語抽屜 */
    .foreign-drawer {{
      margin-top: 16px;
      padding: 16px;
      border-radius: var(--radius-md);
      background: rgba(9, 13, 22, 0.75);
      border: 1px dashed rgba(245, 158, 11, 0.35);
    }}
    .foreign-drawer-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      flex-wrap: wrap;
      gap: 8px;
      font-size: 0.88rem;
      color: #fcd34d;
    }}
    .caption-split {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 12px;
    }}
    .small-label {{
      display: block;
      font-size: 0.76rem;
      color: var(--text-muted);
      margin-bottom: 5px;
      font-weight: 600;
    }}
    textarea.copy-area {{
      width: 100%;
      height: 86px;
      resize: vertical;
      font-size: 0.82rem;
    }}
    .zh-caption-box {{
      background: var(--bg-main);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 9px 12px;
      font-size: 0.82rem;
      white-space: pre-wrap;
      min-height: 86px;
    }}
    .btn-copy {{
      background: var(--emerald);
      color: #06281e;
      border: none;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 0.78rem;
      font-weight: 700;
      cursor: pointer;
    }}

    /* 分頁切換視圖 */
    .tab-pane {{ display: none; }}
    .tab-pane.active {{ display: block; }}

    /* 資料表格 */
    table.clean-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.86rem;
      margin-top: 10px;
    }}
    table.clean-table th, table.clean-table td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border-subtle);
    }}
    table.clean-table th {{
      color: var(--text-muted);
      font-weight: 600;
      font-size: 0.78rem;
      background: rgba(9, 13, 22, 0.4);
    }}

    /* 預覽拆頁卡片 */
    .split-preview-list {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .split-item {{
      background: var(--bg-main);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-sm);
      padding: 10px 12px;
      font-size: 0.82rem;
    }}

    /* 新增帳號 Modal */
    .modal-backdrop {{
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.65);
      align-items: center;
      justify-content: center;
      z-index: 999;
    }}
    .modal-card {{
      background: var(--bg-surface);
      border: 1px solid var(--border-subtle);
      border-radius: var(--radius-lg);
      padding: 24px;
      width: 92%;
      max-width: 460px;
    }}
  </style>
</head>
<body>
  <!-- 左側側邊欄導覽 (Sidebar Dashboard) -->
  <aside class="sidebar">
    <div class="brand-box">
      <div class="brand-avatar">C</div>
      <div>
        <div class="brand-title">Carry Studio</div>
        <div class="brand-sub">TikTok 雙語輪播營運中樞</div>
      </div>
    </div>

    <!-- 多 TikTok 帳號切換器 -->
    <div class="account-switcher">
      <div class="account-switcher-label">
        <span>👤 目前 TikTok 帳號</span>
        <span style="color:#6ee7b7;">● 線上</span>
      </div>
      <select id="sidebarAccountSelect" class="account-select" onchange="switchAccount(this.value)">
        {account_options_html}
      </select>
      <div class="account-focus-note">
        定位：{html.escape(active_account.market_focus)}<br/>
        主視圖：🇹🇼 繁體中文（外語圖預設收合）
      </div>
      <button type="button" class="btn-add-account" onclick="openAccountModal()">➕ 新增 / 綁定 TikTok 帳號</button>
    </div>

    <!-- 側邊欄分頁導覽 -->
    <nav class="nav-menu">
      <button type="button" class="nav-btn active" onclick="switchTab('tab-studio', this)">
        <span>🗂️</span> <span>內容與 Notion 工作台</span>
      </button>
      <button type="button" class="nav-btn" onclick="switchTab('tab-splitter', this)">
        <span>✨</span> <span>智慧內容切分與生圖</span>
      </button>
      <button type="button" class="nav-btn" onclick="switchTab('tab-analytics', this)">
        <span>📊</span> <span>市場研究與數據分析</span>
      </button>
      <button type="button" class="nav-btn" onclick="switchTab('tab-accounts', this)">
        <span>👥</span> <span>多帳號與系統設定</span>
      </button>
    </nav>

    <div class="sidebar-footer">
      <a class="notion-link-btn" href="https://www.notion.so/{html.escape(active_account.notion_database_id)}" target="_blank">
        🔗 開啟 Notion 輪播資料庫
      </a>
      <div>生圖引擎：<code>{html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))}</code></div>
    </div>
  </aside>

  <!-- 右側主畫面 -->
  <main class="main-area">
    <div class="topbar">
      <div>
        <h1 class="topbar-title">{html.escape(active_account.display_name)} <span style="color:#a5b4fc;font-size:1.05rem;">({html.escape(active_account.handle)})</span></h1>
        <div class="topbar-sub">台灣創作者專屬視角：預設展示繁體中文圖片・外語圖片可按需展開・圖片張數依內容智慧切分（3–10 張）</div>
      </div>
      <div class="topbar-actions">
        <button type="button" class="btn-ghost" onclick="toggleAllForeignDrawers()">🌐 全部展開 / 收合越南文圖</button>
        <button type="button" class="btn-ghost" onclick="triggerResearchAnalytics()">📊 更新市場研究＆週報</button>
      </div>
    </div>

    <!-- 即時任務狀態列 -->
    <div id="taskBanner" class="task-toast">
      <div><strong>⚡ 任務狀態：</strong> <span id="taskMessage">{html.escape(str(task_snap['message']))}</span></div>
      <span class="pill pill-slot">免背指令全自動背景執行</span>
    </div>

    <!-- ==================== 分頁 1：內容與 Notion 工作台 ==================== -->
    <section id="tab-studio" class="tab-pane active">
      <div class="kpi-row">
        <div class="kpi-card">
          <div class="kpi-label">目前管理帳號</div>
          <div class="kpi-val">{html.escape(active_account.handle)}（共 {len(all_accounts)} 個帳號）</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">預設圖片檢視語言</div>
          <div class="kpi-val" style="color:#6ee7b7;">🇹🇼 繁體中文優先（外語收合）</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">輪播張數策略</div>
          <div class="kpi-val" style="color:#7dd3fc;">依內容動態切分 (3–10 張)</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">Notion 雲端同步狀態</div>
          <div class="kpi-val" style="color:{'#6ee7b7' if safe_cfg['has_notion_token'] else '#fca5a5'};">
            {'✅ 已連線 (13 欄位同步)' if safe_cfg['has_notion_token'] else '❌ 未設定 Token'}
          </div>
        </div>
      </div>

      <!-- 快速生成列 -->
      <div class="studio-panel">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          <div>
            <h2>🚀 快速生產雙語輪播圖 ＋ 同步至 Notion</h2>
            <div class="muted-hint">可選擇自動依內容切分張數（3–10 張），或切換至「✨ 智慧內容切分與生圖」輸入自訂文案大綱</div>
          </div>
        </div>
        <div class="form-grid">
          <div class="field-box">
            <label for="quickDate">預定發布日期</label>
            <input type="date" id="quickDate" value="{tomorrow_str}"/>
          </div>
          <div class="field-box">
            <label for="quickSlot">發布時段與主題</label>
            <select id="quickSlot">
              <option value="all">全日三時段 (07:00 + 11:30 + 18:00)</option>
              <option value="07:00">07:00 家庭情緒</option>
              <option value="11:30">11:30 台灣生活</option>
              <option value="18:00">18:00 圖文行銷</option>
            </select>
          </div>
          <div class="field-box">
            <label for="quickSlideCount">圖片張數（依內容切分）</label>
            <select id="quickSlideCount">
              <option value="auto" selected>✨ 依內容自動決定張數 (推薦)</option>
              <option value="4">精簡 4 張 (封面 + 2 重點 + CTA)</option>
              <option value="5">標準 5 張 (封面 + 3 重點 + CTA)</option>
              <option value="6">經典 6 張 (封面 + 4 重點 + CTA)</option>
              <option value="7">深度 7 張 (封面 + 5 重點 + CTA)</option>
              <option value="8">完整 8 張 (封面 + 6 重點 + CTA)</option>
            </select>
          </div>
          <div class="field-box">
            <label for="quickEngine">圖像生成引擎</label>
            <select id="quickEngine">
              <option value="codex">ChatGPT CLI 寫實生圖 ({html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))})</option>
              <option value="mock">快速視覺渲染 (立即可看)</option>
            </select>
          </div>
          <div class="field-box" style="justify-content:flex-end;">
            <button type="button" class="btn-primary" onclick="triggerQuickSync()">🚀 一鍵生成並同步 Notion</button>
          </div>
        </div>
      </div>

      <!-- 貼文列表（繁體中文圖優先顯示） -->
      {posts_section}
    </section>

    <!-- ==================== 分頁 2：智慧內容切分與生圖 (Dynamic Slide Splitter) ==================== -->
    <section id="tab-splitter" class="tab-pane">
      <div class="studio-panel">
        <h2>✨ 智慧內容切分器（依內容自動決定生成幾張圖，不再固定 6 張）</h2>
        <div class="muted-hint">
          請輸入你想分享的繁體中文主題與重點（每行一個重點，或一段完整說明）。系統會自動拆解為「第 1 張吸睛封面 ＋ N 張核心重點 ＋ 最後 1 張追蹤收藏 CTA」，自動決定最適合的張數（3–10 張）！
        </div>

        <div class="form-grid" style="margin-top:16px;">
          <div class="field-box">
            <label for="splitDate">預定發布日期</label>
            <input type="date" id="splitDate" value="{tomorrow_str}"/>
          </div>
          <div class="field-box">
            <label for="splitSlot">歸屬時段分類</label>
            <select id="splitSlot">
              <option value="11:30">11:30 台灣生活</option>
              <option value="07:00">07:00 家庭情緒</option>
              <option value="18:00">18:00 圖文行銷</option>
            </select>
          </div>
          <div class="field-box">
            <label for="splitCountMode">張數切分模式</label>
            <select id="splitCountMode">
              <option value="auto" selected>✨ 依輸入內容自動決定幾張圖 (3–10 張)</option>
              <option value="3">固定切為 3 張</option>
              <option value="4">固定切為 4 張</option>
              <option value="5">固定切為 5 張</option>
              <option value="6">固定切為 6 張</option>
              <option value="7">固定切為 7 張</option>
              <option value="8">固定切為 8 張</option>
              <option value="9">固定切為 9 張</option>
              <option value="10">固定切為 10 張</option>
            </select>
          </div>
          <div class="field-box">
            <label for="splitEngine">生圖引擎</label>
            <select id="splitEngine">
              <option value="mock">⚡ 快速視覺渲染 (3 秒預覽成果)</option>
              <option value="codex">✨ ChatGPT CLI 寫實生圖 ({html.escape(str(safe_cfg.get('codex_model', 'gpt-6-sol')))})</option>
            </select>
          </div>
        </div>

        <div class="field-box" style="margin-top:14px;">
          <label for="customContentInput">輸入想生成的繁體中文主題與各段重點（每行一點，系統將自動切分頁數並翻譯對照越南文）</label>
          <textarea id="customContentInput" rows="6" placeholder="範例（輸入 3 個重點會自動切分為 5 張圖：封面 + 3 重點 + 尾頁 CTA）：&#10;台灣租屋簽約必看指南&#10;看懂押金規定：依法最高不超過兩個月租金&#10;確認水電費計算方式：台水台電帳單或一度幾元先寫清楚&#10;入住前拍照存證：家具家電現況先拍照保護雙方權益">台灣夜市省錢必吃攻略
先看在地人排隊攤位：跟著台灣學生與上班族排隊最不踩雷
善用行動支付與零錢：準備好百元鈔與零錢結帳最快
與朋友一起分食多樣小吃：一次品嚐大腸包小腸、地瓜球與木瓜牛奶</textarea>
        </div>

        <div style="display:flex;gap:12px;margin-top:16px;flex-wrap:wrap;">
          <button type="button" class="btn-ghost" onclick="previewContentSplit()">🔍 第一步：即時預覽會切分成幾張圖</button>
          <button type="button" class="btn-primary" onclick="triggerCustomSplitSync()">🎨 第二步：依此切分張數生成圖片 ＋ 同步 Notion</button>
        </div>

        <div id="splitPreviewContainer" style="margin-top:18px;display:none;">
          <div style="font-weight:700;color:#7dd3fc;margin-bottom:6px;" id="splitPreviewSummary"></div>
          <div id="splitPreviewCards" class="split-preview-list"></div>
        </div>
      </div>
    </section>

    <!-- ==================== 分頁 3：市場研究與數據分析 ==================== -->
    <section id="tab-analytics" class="tab-pane">
      <div class="studio-panel">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
          <div>
            <h2>📈 Phase 5｜三時段成效對照與下週選題評分權重建議</h2>
            <div class="muted-hint">A/B 推薦領先變體：{html.escape(str(opt_proposal.ab_test_summary.get('recommended_lead_variant') or 'A_LIST_HOOK'))}</div>
          </div>
          <button type="button" class="btn-primary" onclick="triggerResearchAnalytics()">🔄 重新執行研究與週報分析</button>
        </div>
        <div style="margin:14px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
          <span style="font-size:0.84rem;color:var(--text-muted);">下週建議選題權重：</span>
          {weight_badges_html}
        </div>
        <table class="clean-table">
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

      <div class="studio-panel">
        <h2>🔍 Phase 2｜越南市場熱門搜尋關鍵字字典（共 {len(keywords)} 組關鍵字 ／ {len(cases)} 篇研究案例）</h2>
        <table class="clean-table">
          <thead>
            <tr>
              <th>越南文搜尋關鍵字</th>
              <th>繁體中文對照</th>
              <th>搜尋意圖分類</th>
              <th>建議發布時段</th>
            </tr>
          </thead>
          <tbody>
            {keywords_rows_html}
          </tbody>
        </table>
      </div>
    </section>

    <!-- ==================== 分頁 4：多帳號與系統設定 ==================== -->
    <section id="tab-accounts" class="tab-pane">
      <div class="studio-panel">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
          <div>
            <h2>👥 多 TikTok 帳號管理中心</h2>
            <div class="muted-hint">單一工作台可綁定並切換多個 TikTok 帳號，各自擁有定位、目標語言與 Notion 資料庫</div>
          </div>
          <button type="button" class="btn-primary" onclick="openAccountModal()">➕ 新增 TikTok 帳號</button>
        </div>
        <table class="clean-table">
          <thead>
            <tr>
              <th>TikTok 帳號 Handle</th>
              <th>顯示名稱與品牌線</th>
              <th>受眾與內容定位</th>
              <th>語言配置</th>
              <th>Notion 資料庫 ID</th>
              <th>狀態 / 切換</th>
            </tr>
          </thead>
          <tbody>
            {accounts_table_rows}
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <!-- 新增 TikTok 帳號 Modal -->
  <div id="accountModal" class="modal-backdrop">
    <div class="modal-card">
      <h3 style="margin:0 0 12px 0;">➕ 新增 / 綁定 TikTok 帳號</h3>
      <div class="field-box" style="margin-bottom:10px;">
        <label>帳號識別碼 (英文小寫與連字號)</label>
        <input type="text" id="newAccId" placeholder="例如：carry-vn-beauty"/>
      </div>
      <div class="field-box" style="margin-bottom:10px;">
        <label>TikTok 帳號 Handle</label>
        <input type="text" id="newAccHandle" placeholder="例如：@carry.vn.beauty"/>
      </div>
      <div class="field-box" style="margin-bottom:10px;">
        <label>顯示名稱 / 品牌名稱</label>
        <input type="text" id="newAccName" placeholder="例如：Carry | 台越美妝保養筆記"/>
      </div>
      <div class="field-box" style="margin-bottom:10px;">
        <label>目標受眾與內容定位</label>
        <input type="text" id="newAccFocus" placeholder="例如：台灣開架美妝、生活好物推薦"/>
      </div>
      <div class="field-box" style="margin-bottom:16px;">
        <label>綁定 Notion 資料庫 ID</label>
        <input type="text" id="newAccNotionDb" value="{html.escape(active_account.notion_database_id)}"/>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:10px;">
        <button type="button" class="btn-ghost" onclick="closeAccountModal()">取消</button>
        <button type="button" class="btn-primary" onclick="createNewAccount()">儲存並切換帳號</button>
      </div>
    </div>
  </div>

  <script>
    function switchTab(tabId, btnEl) {{
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      const target = document.getElementById(tabId);
      if (target) target.classList.add('active');
      if (btnEl) btnEl.classList.add('active');
    }}

    function toggleForeignDrawer(drawerId, btnEl) {{
      const el = document.getElementById(drawerId);
      if (!el) return;
      const isHidden = el.style.display === 'none' || !el.style.display;
      el.style.display = isHidden ? 'block' : 'none';
      if (btnEl) {{
        btnEl.textContent = isHidden
          ? '🔽 收合越南文圖片與外語貼文（省空間）'
          : '👁️ 展開越南文圖片與外語貼文（預設收合省空間）';
      }}
    }}

    let allViExpanded = false;
    function toggleAllForeignDrawers() {{
      allViExpanded = !allViExpanded;
      document.querySelectorAll('.foreign-drawer').forEach(el => {{
        el.style.display = allViExpanded ? 'block' : 'none';
      }});
    }}

    function copyText(elementId) {{
      const el = document.getElementById(elementId);
      if (!el) return;
      el.select();
      navigator.clipboard.writeText(el.value);
      alert('✅ 已複製越南文貼文與 5 個 Hashtag！可直接貼到 TikTok 發布。');
    }}

    async function switchAccount(accountId) {{
      await fetch('/api/accounts/switch', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{account_id: accountId}})
      }});
      window.location.href = '/?account=' + encodeURIComponent(accountId);
    }}

    function openAccountModal() {{
      document.getElementById('accountModal').style.display = 'flex';
    }}
    function closeAccountModal() {{
      document.getElementById('accountModal').style.display = 'none';
    }}

    async function createNewAccount() {{
      const accId = document.getElementById('newAccId').value.trim();
      const handle = document.getElementById('newAccHandle').value.trim();
      const name = document.getElementById('newAccName').value.trim();
      const focus = document.getElementById('newAccFocus').value.trim();
      const notionDb = document.getElementById('newAccNotionDb').value.trim();
      if (!handle || !name) {{
        alert('請填寫 TikTok 帳號 Handle 與顯示名稱！');
        return;
      }}
      const res = await fetch('/api/accounts/create', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          account_id: accId,
          handle: handle,
          display_name: name,
          market_focus: focus || '在台越南受眾內容',
          notion_database_id: notionDb
        }})
      }});
      const data = await res.json();
      if (data.ok) {{
        window.location.href = '/?account=' + encodeURIComponent(data.account_id);
      }}
    }}

    async function previewContentSplit() {{
      const content = document.getElementById('customContentInput').value;
      const slot = document.getElementById('splitSlot').value;
      const slideCount = document.getElementById('splitCountMode').value;
      const accountId = document.getElementById('sidebarAccountSelect').value;

      const res = await fetch('/api/preview-split', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          content: content,
          slot: slot,
          slide_count: slideCount,
          account_id: accountId
        }})
      }});
      const data = await res.json();
      const box = document.getElementById('splitPreviewContainer');
      const summary = document.getElementById('splitPreviewSummary');
      const cards = document.getElementById('splitPreviewCards');
      box.style.display = 'block';
      summary.textContent = `✨ 智慧切分結果：依您的內容共切分為 ${{data.total_slides}} 張輪播圖（含封面 + ${{data.total_slides - 2}} 個核心重點 + 尾頁 CTA）`;
      cards.innerHTML = data.slides.map(s => `
        <div class="split-item">
          <div style="color:#a5b4fc;font-weight:700;margin-bottom:4px;">第 ${{String(s.index).padStart(2, '0')}} / ${{String(data.total_slides).padStart(2, '0')}} 張</div>
          <div style="color:#f1f5f9;font-weight:600;">🇹🇼 ${{s.zh_tw}}</div>
          <div style="color:#94a3b8;font-size:0.76rem;margin-top:4px;">🇻🇳 ${{s.vi_head}} — ${{s.vi_body}}</div>
        </div>
      `).join('');
    }}

    async function triggerQuickSync() {{
      const dateVal = document.getElementById('quickDate').value;
      const slotVal = document.getElementById('quickSlot').value;
      const engineVal = document.getElementById('quickEngine').value;
      const slideCountVal = document.getElementById('quickSlideCount').value;
      const accountId = document.getElementById('sidebarAccountSelect').value;
      const res = await fetch('/api/run-sync', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          date: dateVal,
          slot: slotVal,
          engine: engineVal,
          slide_count: slideCountVal,
          account_id: accountId,
          custom_content: ''
        }})
      }});
      const data = await res.json();
      document.getElementById('taskMessage').textContent = data.message || '已啟動任務';
    }}

    async function triggerCustomSplitSync() {{
      const dateVal = document.getElementById('splitDate').value;
      const slotVal = document.getElementById('splitSlot').value;
      const engineVal = document.getElementById('splitEngine').value;
      const slideCountVal = document.getElementById('splitCountMode').value;
      const contentVal = document.getElementById('customContentInput').value;
      const accountId = document.getElementById('sidebarAccountSelect').value;
      const res = await fetch('/api/run-sync', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{
          date: dateVal,
          slot: slotVal,
          engine: engineVal,
          slide_count: slideCountVal,
          account_id: accountId,
          custom_content: contentVal
        }})
      }});
      const data = await res.json();
      document.getElementById('taskMessage').textContent = data.message || '已啟動自訂切分生圖任務';
      switchTab('tab-studio', document.querySelector('.nav-btn'));
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
    """建立支援多帳號管理、動態內容切分預覽、圖片讀取與任務觸發的 HTTP Handler。"""

    class DashboardHandler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, payload_obj: dict[str, object]) -> None:
            body = json.dumps(payload_obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            query_params = urllib.parse.parse_qs(parsed.query)

            if route in {"/", "/index.html"}:
                selected_acc = (
                    query_params.get("account", [None])[0]
                    if "account" in query_params
                    else None
                )
                body = render_dashboard_html(
                    project_root, settings, selected_account_id=selected_acc
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if route == "/api/status":
                self._send_json(200, settings.safe_summary())
                return

            if route == "/api/task-status":
                self._send_json(200, get_task_state_snapshot())
                return

            if route == "/api/accounts":
                repo = PostStateRepository(settings.sqlite_db_path)
                self._send_json(
                    200,
                    {
                        "active": repo.get_active_account().model_dump(),
                        "accounts": [a.model_dump() for a in repo.list_accounts()],
                    },
                )
                return

            if route.startswith("/media/"):
                rel_path = urllib.parse.unquote(route.removeprefix("/media/"))
                target_file = (project_root / rel_path).resolve()
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
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            params = json.loads(raw_body) if raw_body.strip() else {}

            if route == "/api/accounts/switch":
                repo = PostStateRepository(settings.sqlite_db_path)
                acc_id = str(params.get("account_id") or "carry-vietnam-tw")
                active = repo.set_active_account(acc_id)
                self._send_json(200, {"ok": True, "active": active.model_dump()})
                return

            if route == "/api/accounts/create":
                repo = PostStateRepository(settings.sqlite_db_path)
                handle = str(params.get("handle") or "@carry.new")
                raw_id = str(params.get("account_id") or handle.lstrip("@"))
                clean_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw_id).strip("-").lower() or "custom-account"
                profile = TikTokAccountProfile(
                    account_id=clean_id,
                    handle=handle if handle.startswith("@") else f"@{handle}",
                    display_name=str(params.get("display_name") or handle),
                    market_focus=str(params.get("market_focus") or "在台越南人生活與成長"),
                    notion_database_id=str(
                        params.get("notion_database_id") or settings.notion_database_id
                    ),
                    is_active=True,
                )
                repo.upsert_account(profile)
                self._send_json(200, {"ok": True, "account_id": clean_id})
                return

            if route == "/api/preview-split":
                repo = PostStateRepository(settings.sqlite_db_path)
                acc_id = str(params.get("account_id") or "")
                active = repo.set_active_account(acc_id) if acc_id else repo.get_active_account()
                slot_str = str(params.get("slot") or "11:30")
                slot_enum = {
                    "07:00": ContentSlot.MORNING_0700,
                    "11:30": ContentSlot.NOON_1130,
                    "18:00": ContentSlot.EVENING_1800,
                }.get(slot_str, ContentSlot.NOON_1130)
                sc_str = str(params.get("slide_count") or "auto")
                target_sc = int(sc_str) if sc_str.isdigit() else None
                slides_raw = split_content_into_slides(
                    str(params.get("content") or ""),
                    slot=slot_enum,
                    account_handle=active.handle,
                    target_slide_count=target_sc,
                )
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "total_slides": len(slides_raw),
                        "slides": [
                            {
                                "index": i,
                                "vi_head": vh,
                                "vi_body": vb,
                                "zh_tw": zf,
                            }
                            for i, (vh, vb, zf) in enumerate(slides_raw, start=1)
                        ],
                    },
                )
                return

            snap = get_task_state_snapshot()
            if snap.get("running"):
                self._send_json(
                    409,
                    {"ok": False, "message": "目前已有任務正在執行中，請稍候完成。"},
                )
                return

            if route == "/api/run-sync":
                target_date = str(params.get("date") or "2026-10-21")
                slot_choice = str(params.get("slot") or "all")
                engine_choice = str(params.get("engine") or "codex")
                account_id = str(params.get("account_id") or "carry-vietnam-tw")
                custom_content = str(params.get("custom_content") or "")
                slide_count_choice = str(params.get("slide_count") or "auto")

                t = threading.Thread(
                    target=_run_sync_notion_job,
                    args=(
                        project_root,
                        settings,
                        target_date,
                        slot_choice,
                        engine_choice,
                        account_id,
                        custom_content,
                        slide_count_choice,
                    ),
                    daemon=True,
                )
                t.start()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "message": f"🚀 已啟動 {target_date} ({slot_choice}) 內容切分與雙語輪播圖生成任務！",
                    },
                )
                return

            if route == "/api/run-research-analytics":
                t = threading.Thread(
                    target=_run_research_and_analytics_job,
                    args=(project_root, settings),
                    daemon=True,
                )
                t.start()
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "message": "📊 已啟動市場研究與每週權重優化分析任務！",
                    },
                )
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
    """啟動本機現代化側邊欄 Web GUI 伺服器並自動開啟瀏覽器。"""
    root = (project_root or Path.cwd()).resolve()
    cfg = settings or AppSettings()
    handler_cls = create_dashboard_handler(root, cfg)
    server = HTTPServer((host, port), handler_cls)
    url = f"http://{host}:{port}"
    print(f"🚀 Carry Studio 多帳號圖形化控制台已啟動：{url}")
    print("   （瀏覽器已自動開啟，按 Ctrl+C 可關閉控制台）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
