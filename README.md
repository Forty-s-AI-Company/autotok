# AutoTok

TikTok 多帳號輪播圖與文案的研究、製作、審稿與發布工作台。個人自用，在本機執行。

- 多個 TikTok 帳號，各自設定發布語言與市場（時區、每日發文時間、受眾、合規提醒）
- 搜尋並整理爆款案例，依帳號定位算出「適合這個帳號」的排序
- 依選題與帳號市場產生逐頁文案，生成 4:5（1080×1350）獨立圖片，附繁體中文對照供審稿
- 品質檢查通過後同步到 Notion（每個帳號各自綁定資料庫）
- 每個資料區塊都顯示最後更新時間與來源；模擬資料一律標示

## 開始使用

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp -n .env.example .env
uv run python run.py          # 開啟 http://127.0.0.1:8765
```

macOS 可雙擊 `啟動控制台.command`，Windows 雙擊 `啟動控制台.bat`。

第一次使用的順序：

1. 設定 → 新增帳號，選市場範本並調整
2. 設定 → Notion，登入並選擇要寫入的資料庫
3. 研究 → 重新研究，挑一個適合的題目送去製作
4. 製作 → 審稿，通過品質檢查後同步到 Notion

## 發布的限制

TikTok 官方 API 在 App 通過審核前只能發為「僅自己可見」，或匯出圖片與文案自行發布。各帳號目前的發布能力顯示在 設定 → TikTok。系統不使用非官方上傳工具。詳見 [docs/RISKS.md](docs/RISKS.md)。

## 開發

```bash
uv run pytest -q
uv run ruff check .
uv run autotok-vn --help      # 命令列工具（dry-run 發布、驗證圖片等）
```

文件：[架構](docs/ARCHITECTURE.md) · [設定與備援機](docs/SETUP.md) · [決策紀錄](docs/DECISIONS.md) · [風險](docs/RISKS.md) · [原始越南市場規格](docs/MASTER_SPEC.md)
