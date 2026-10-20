"""系統狀態：把各來源的「最後更新」與健康度整理成畫面可直接使用的清單。"""

from __future__ import annotations

from app.config import AppSettings
from app.engines import codex_status
from app.scheduler import tick_age_minutes
from app.state import PostStateRepository


def system_status(repo: PostStateRepository, settings: AppSettings, account_id: str) -> list[dict[str, object]]:
    """每筆：name、level(ok|warn|err|off)、ts(ISO 或 None)、text、stale_min、href。"""
    rows: list[dict[str, object]] = []

    notion = repo.get_notion_binding(account_id)
    has_env_token = bool(settings.notion_token.strip())
    if notion and notion.get("database_id"):
        missing = notion.get("schema_missing") or []
        rows.append(
            {
                "name": "Notion",
                "level": "warn" if missing else "ok",
                "ts": notion.get("verified_at"),
                "text": f"缺 {len(missing)} 個欄位" if missing else "已綁定",
                "stale_min": 1440,
            }
        )
    elif has_env_token:
        rows.append({"name": "Notion", "level": "warn", "ts": None, "text": "僅有全域 Token，尚未綁定資料庫", "stale_min": 0})
    else:
        rows.append({"name": "Notion", "level": "off", "ts": None, "text": "未綁定", "stale_min": 0})

    research = repo.last_job(account_id=account_id, job="research", only_ok=True)
    rows.append(
        {
            "name": "爆款研究",
            "level": "ok" if research else "off",
            "ts": research["finished_at"] if research else None,
            "text": "" if research else "尚未執行",
            "stale_min": 1440,
        }
    )

    engine = codex_status(settings.codex_bin)
    scan = repo.last_job(account_id="", job="engine_check")
    if not engine.ok:
        rows.append({"name": "生圖與文案引擎", "level": "err", "ts": None, "text": "找不到 Codex CLI", "stale_min": 0})
    elif scan is None or scan["status"] == "running":
        rows.append({"name": "生圖與文案引擎", "level": "off", "ts": None, "text": "掃描中…", "stale_min": 0})
    elif scan["status"] == "error":
        rows.append({"name": "生圖與文案引擎", "level": "err", "ts": scan["finished_at"], "text": "模型可能已失效", "stale_min": 0})
    else:
        level = str(scan["summary"]).partition("|")[0]
        rows.append({"name": "生圖與文案引擎", "level": "ok" if level == "ok" else "warn", "ts": scan["finished_at"], "text": "", "stale_min": 0})

    tiktok = repo.get_tiktok_binding(account_id)
    cap = str(tiktok["publish_capability"]) if tiktok else "export_only"
    rows.append(
        {
            "name": "TikTok 發布",
            "level": "ok" if cap == "public" else "warn",
            "ts": tiktok.get("verified_at") if tiktok else None,
            "text": {"public": "可公開發布", "self_only": "僅自己可見", "export_only": "僅匯出"}.get(cap, cap),
            "stale_min": 0,
        }
    )
    if any(repo.get_market_profile(a.account_id).auto_enabled for a in repo.list_accounts()):
        tick = repo.last_job(account_id="", job="scheduler_tick")
        age = tick_age_minutes(repo)
        if tick is None or age is None:
            rows.append({"name": "自動排程", "level": "warn", "ts": None, "text": "尚未執行過", "stale_min": 0})
        else:
            stale = age > 30
            rows.append({"name": "自動排程", "level": "warn" if stale else "ok", "ts": tick["started_at"], "text": "", "stale_min": 30})
    return rows
