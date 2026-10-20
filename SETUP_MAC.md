# macOS 開發環境設定與操作指南（SETUP_MAC.md）

> 適用於 macOS（Apple Silicon / Intel）。嚴格遵循使用 `uv` 與 `.venv` 管理虛擬環境，絕不修改系統預設 Python 或使用全域 `pip`。

---

## 1. 環境需求與檢查

本專案於以下 macOS 環境完成實測驗證：
- **作業系統**：macOS 26.7 (Build 25G229)
- **套件與環境管理**：`uv` 0.13.3+（安裝路徑：`~/.local/bin/uv`）
- **Python 版本**：CPython 3.12.15（由 `uv` 自動下載並綁定於專案 `.venv`）

若您的終端機尚未將 `~/.local/bin` 加入 `PATH`，請先執行：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

（若尚未安裝 `uv`，可執行 `curl -LsSf https://astral.sh/uv/install.sh | sh`。）

---

## 2. 安裝與同步虛擬環境

在專案根目錄 `/Users/eden/Documents/AI/autotok` 執行：

```bash
# 安裝 Python 3.12 並建立 .venv 虛擬環境、安裝所有依賴套件
uv python install 3.12
uv sync
```

建立本地環境設定檔（預設 `ALLOW_LIVE_PUBLISH=false`，安全無外部副作用）：

```bash
cp -n .env.example .env
```

---

## 3. 可重現的驗證與執行命令

### 3.1 執行 Ruff 靜態檢查與 Pytest 測試套件
```bash
uv run ruff check .
uv run pytest -v
```

### 3.2 產生 6 張獨立 1080×1350 測試 JPEG（標記 TEST/MOCK，順序 01–06）
```bash
uv run python -m app generate-mock-media artifacts/mock_media/post_001 --post-id mock-post-001
```

### 3.3 執行媒體驗證器（Quality Gate）
```bash
uv run python -m app verify-media artifacts/mock_media/post_001
```
- 若目錄內恰好有 6 張 1080×1350 真實 JPEG、無重複、無 PNG 且頁碼為 `01–06`，將輸出 `"passed": true` 並回傳 Exit Code `0`。
- 若有任何不合規（如混入 PNG、尺寸不符、缺圖或重複圖），將輸出詳細錯誤原因並回傳 Exit Code `1`。

### 3.4 執行安全 Dry-Run 發布預覽
```bash
# 預覽官方 TikTok Content Posting API (PHOTO / PULL_FROM_URL) Payload
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter official_api --dry-run

# 預覽半自動匯出模式 (manual_export)
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter manual_export --dry-run

# 驗證 autotok_candidate 會明確回報 UNSUPPORTED_FEATURE (Exit Code 2)
uv run python -m app publish --media-dir artifacts/mock_media/post_001 --adapter autotok_candidate --dry-run
```
