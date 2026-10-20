"""設定管理與敏感資訊遮罩工具。

使用 Pydantic Settings 讀取環境變數，並提供日誌遮罩（Log Redaction）防止憑證外洩。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AdapterName = Literal["mock", "official_api", "autotok_candidate", "manual_export"]

SENSITIVE_PATTERNS = [
    re.compile(r"(sessionid|access_token|client_secret|notion_token|secret_[a-z_]+)\s*[:=]\s*([^\s,;\"']+)", re.IGNORECASE),
    re.compile(r"(Bearer\s+)([A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
]


def redact_sensitive(text: str) -> str:
    """將文字中的 Token、Session ID 與 Secret 遮罩為 [REDACTED]，防止寫入日誌或終端機。"""
    result = text
    for pattern in SENSITIVE_PATTERNS:
        result = pattern.sub(r"\1=[REDACTED]", result)
    return result


class AppSettings(BaseSettings):
    """應用程式全域設定（支援 .env 與環境變數覆寫）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    timezone: str = Field(default="Asia/Taipei", alias="TIMEZONE")
    sqlite_db_path: Path = Field(default=Path("data/autotok_state.db"), alias="SQLITE_DB_PATH")
    export_base_dir: Path = Field(default=Path("artifacts/manual_exports"), alias="EXPORT_BASE_DIR")

    allow_live_publish: bool = Field(default=False, alias="ALLOW_LIVE_PUBLISH")
    default_uploader_adapter: AdapterName = Field(default="mock", alias="DEFAULT_UPLOADER_ADAPTER")

    # TikTok Official API
    tiktok_client_key: str = Field(default="", alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str = Field(default="", alias="TIKTOK_CLIENT_SECRET")
    tiktok_access_token: str = Field(default="", alias="TIKTOK_ACCESS_TOKEN")
    tiktok_verified_image_url_prefix: str = Field(
        default="", alias="TIKTOK_VERIFIED_IMAGE_URL_PREFIX"
    )
    tiktok_app_audited: bool = Field(default=False, alias="TIKTOK_APP_AUDITED")

    # Notion (Phase 2/4 整合預留)
    notion_token: str = Field(default="", alias="NOTION_TOKEN")
    notion_database_id: str = Field(
        default="f1bc34396f154e62bca90c9deea19d7a", alias="NOTION_DATABASE_ID"
    )
    notion_data_source_id: str = Field(
        default="4d575a73-4f98-4ff6-b3f8-da68c40c7125", alias="NOTION_DATA_SOURCE_ID"
    )

    def safe_summary(self) -> dict[str, object]:
        """傳回已遮罩敏感欄位的設定摘要，供報表與除錯使用。"""
        return {
            "app_env": self.app_env,
            "timezone": self.timezone,
            "sqlite_db_path": str(self.sqlite_db_path),
            "export_base_dir": str(self.export_base_dir),
            "allow_live_publish": self.allow_live_publish,
            "default_uploader_adapter": self.default_uploader_adapter,
            "has_tiktok_client_key": bool(self.tiktok_client_key.strip()),
            "has_tiktok_access_token": bool(self.tiktok_access_token.strip()),
            "tiktok_verified_image_url_prefix": self.tiktok_verified_image_url_prefix,
            "tiktok_app_audited": self.tiktok_app_audited,
            "has_notion_token": bool(self.notion_token.strip()),
        }
