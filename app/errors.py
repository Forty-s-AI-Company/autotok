"""系統自訂例外與狀態錯誤定義。

嚴格區分「功能不支援」、「缺少授權或審核阻礙」、「媒體驗證失敗」與「真實執行風險」，
確保在未通過驗證時絕不偽裝成功。
"""

from __future__ import annotations


class AutotokSystemError(Exception):
    """系統基礎例外類別。"""

    def __init__(self, message: str, *, code: str = "SYSTEM_ERROR") -> None:
        super().__init__(message)
        self.code = code


class UnsupportedFeatureError(AutotokSystemError):
    """目標通道或 Adapter 原生不支援指定功能（例如 autotok 不支援六張照片輪播）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNSUPPORTED_FEATURE")


class BlockedNeedsAuthError(AutotokSystemError):
    """需要使用者外部帳號授權、OAuth 憑證或安全開關（ALLOW_LIVE_PUBLISH）才能執行。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BLOCKED_NEEDS_AUTH")


class BlockedNeedsAuditError(AutotokSystemError):
    """官方 API 尚未通過 TikTok App Audit，無法進行公開發布（PUBLIC_TO_EVERYONE）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BLOCKED_NEEDS_AUDIT")


class MediaValidationError(AutotokSystemError):
    """六張照片輪播品質或格式驗證失敗（尺寸、數量、JPEG MIME、重複或排序錯誤）。"""

    def __init__(self, message: str, *, reasons: list[str] | None = None) -> None:
        super().__init__(message, code="QUALITY_FAILED")
        self.reasons = reasons or [message]
