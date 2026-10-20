# 第五階段完成報告（PHASE5_REPORT.md）

版本：v1.0｜日期：2026-10-20｜狀態：**Phase 5 核心架構與測試完成（全系統 Phase 1–5 共 31 項測試全數通過）**

---

## 1. 交付目標與規格對齊

依據 [`MASTER_SPEC.md`](MASTER_SPEC.md) 第 4、5.4、7、11 節規範，Phase 5 完成以下核心模組：

1. **成效資料來源層級與歸因可信度模型（[`app/models.py`](app/models.py)）**：
   - `AnalyticsSourceTier`：嚴格區分 `CREATOR_BACKEND_VERIFIED`（創作者後台或官方授權 API 可驗證數據）、`PUBLIC_OBSERVED`（公開頁面數據）、`ESTIMATED_OR_UNVERIFIED` 與 `MOCK_SIMULATION`。
   - `AttributionConfidence`：嚴格區分 `DIRECT_POST_ATTRIBUTION`（單篇後台直接歸因）、`TIME_WINDOW_CORRELATION`（僅時段相關性）與 `UNAVAILABLE`（無歸因資料），防止將「高觀看」直接等同於「帶粉」。
   - `PostPerformanceObservation`：所有互動指標（`views`, `likes`, `comments`, `shares`, `saves`, `attributed_new_followers`）在缺失時皆保留為 `None`（SQLite `NULL`），絕不預設填 `0`。

2. **口徑分離、A/B 實驗對照與下週選題權重優化引擎（[`app/analytics.py`](app/analytics.py)）**：
   - [`compute_post_observation_metrics()`](app/analytics.py#L46-L108)：自動判斷完整互動率（`FULL`）與部分互動率（`PARTIAL`），並在彙整時禁止跨口徑直接混算平均。
   - [`AnalyticsAgent.summarize_by_slot()`](app/analytics.py#L228-L287)：分別統計 `07:00_EMOTION_GROWTH`、`11:30_TAIWAN_LIFE_CULTURE`、`18:00_CREATOR_DIGITAL_GROWTH` 三時段的完整互動率、收藏分享比與經驗證之新增粉絲數。
   - [`AnalyticsAgent.evaluate_ab_variants()`](app/analytics.py#L289-L343)：對照 `A_LIST_HOOK`（數字清單鉤子）與 `B_STORY_HOOK`（情境故事鉤子）之收藏率與直接帶粉轉換率。
   - [`AnalyticsAgent.generate_weekly_optimization_proposal()`](app/analytics.py#L345-L418)：根據實測收藏分享比與直接帶粉表現，對內部選題研究性評分權重（預設 `35% / 25% / 20% / 10% / 10%`）提出受控微調建議（確保總和恆為 `1.00`），並自動輸出 Markdown 報告（[`artifacts/analytics/weekly_optimization_report.md`](artifacts/analytics/weekly_optimization_report.md)）。

3. **SQLite 持久化與 CLI / Web GUI 整合（[`app/state.py`](app/state.py)、[`app/cli.py`](app/cli.py)、[`app/gui.py`](app/gui.py)）**：
   - 新增 `post_observations` 資料表與 `upsert_post_observation` / `list_post_observations`。
   - 新增 CLI 命令：
     ```bash
     # 演練模式（使用標記 is_mock=True 之離線觀察樣本）
     uv run python -m app analytics-report --mode mock

     # 真實 JSON 匯入模式（載入創作者後台或公開擷取之觀察指標）
     uv run python -m app analytics-report --mode json --input-json path/to/observations.json
     ```

---

## 2. 測試驗證結果

執行命令：
```bash
uv run ruff check .
uv run pytest -v
```
- **Ruff 靜態檢查**：`All checks passed!`
- **Pytest 自動化測試**：Phase 1（11 項）+ Phase 2（7 項）+ Phase 3（5 項）+ Phase 4（4 項）+ Phase 5（4 項）= **31 項測試全數通過（31 passed）**。
