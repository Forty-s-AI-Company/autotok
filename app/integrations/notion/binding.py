"""每帳號 Notion 憑證：Token 存金鑰圈，SQLite 只存參照。預設帳號可退回全域 NOTION_TOKEN。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app import credentials
from app.config import AppSettings
from app.models import DEFAULT_ACCOUNT_ID


@dataclass(frozen=True)
class AccountCredentials:
    token: str
    database_id: str
    source: Literal["account", "global"]


def token_ref(account_id: str) -> str:
    return f"notion:{account_id}"


def resolve_token(repo: Any, settings: AppSettings, account_id: str) -> tuple[str, Literal["account", "global"]] | None:
    """回傳 (token, 來源)。帳號專屬 Token 優先；只有預設帳號可退回全域 Token。"""
    binding = repo.get_notion_binding(account_id)
    ref = str(binding["token_ref"]) if binding and binding.get("token_ref") else ""
    if ref:
        token = credentials.get_secret(ref)
        if token:
            return token, "account"
    if account_id == DEFAULT_ACCOUNT_ID and settings.notion_token.strip():
        return settings.notion_token.strip(), "global"
    return None


def resolve_account_credentials(repo: Any, settings: AppSettings, account_id: str) -> AccountCredentials | None:
    found = resolve_token(repo, settings, account_id)
    if found is None:
        return None
    token, source = found
    binding = repo.get_notion_binding(account_id)
    database_id = str(binding["database_id"]) if binding and binding.get("database_id") else ""
    if not database_id and source == "global":
        database_id = settings.notion_database_id.strip()
    if not database_id:
        return None
    return AccountCredentials(token=token, database_id=database_id, source=source)


def store_connection(
    repo: Any, account_id: str, *, token: str, auth_method: str, workspace_name: str
) -> None:
    """保存 Token（金鑰圈）與連線資訊（SQLite）。換連線時清掉舊的資料庫綁定。"""
    credentials.set_secret(token_ref(account_id), token)
    repo.save_notion_binding(
        account_id,
        auth_method=auth_method,
        workspace_name=workspace_name,
        token_ref=token_ref(account_id),
        database_id="",
        database_title="",
        parent_page_id="",
        schema_missing=[],
        schema_checked_at=None,
        verified_at=datetime.now(UTC).isoformat(),
    )


def clear_connection(repo: Any, account_id: str) -> None:
    credentials.delete_secret(token_ref(account_id))
    repo.clear_notion_binding(account_id)


def find_database_owner(repo: Any, settings: AppSettings, database_id: str, exclude_account_id: str) -> str | None:
    """回傳已綁定同一個資料庫的其他帳號 handle；沒有則 None。

    共用資料庫會讓兩個帳號的頁面互相覆蓋（同步以日期＋時段尋找既有頁面），所以不允許。
    """
    target = database_id.replace("-", "").lower()
    for acc in repo.list_accounts():
        if acc.account_id == exclude_account_id:
            continue
        binding = repo.get_notion_binding(acc.account_id)
        effective = str(binding["database_id"]) if binding and binding.get("database_id") else ""
        if not effective and acc.account_id == DEFAULT_ACCOUNT_ID and settings.notion_token.strip():
            effective = settings.notion_database_id.strip()
        if effective and effective.replace("-", "").lower() == target:
            return str(acc.handle)
    return None
