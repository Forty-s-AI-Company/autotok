"""系統核心資料模型與狀態機定義。

遵循 MASTER_SPEC.md 第 7 與第 8 節規範，支援完整生命週期狀態機與未來擴充（Research / Topic / Post）。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PostStatus(StrEnum):
    """貼文狀態機列舉（對應 MASTER_SPEC.md 第 8 節）。"""

    DRAFT = "DRAFT"
    RESEARCHED = "RESEARCHED"
    GENERATING = "GENERATING"
    READY_TO_REVIEW = "READY_TO_REVIEW"
    QUALITY_FAILED = "QUALITY_FAILED"
    READY = "READY"
    SCHEDULED = "SCHEDULED"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    BLOCKED_NEEDS_AUTH = "BLOCKED_NEEDS_AUTH"


class ContentSlot(StrEnum):
    """每日固定三時段與對應主題分類（Asia/Taipei）。"""

    MORNING_0700 = "07:00_EMOTION_GROWTH"
    NOON_1130 = "11:30_TAIWAN_LIFE_CULTURE"
    EVENING_1800 = "18:00_CREATOR_DIGITAL_GROWTH"


class SlideItem(BaseModel):
    """單頁輪播圖文結構（01–06）。"""

    index: int = Field(ge=1, le=6, description="頁碼 1 到 6")
    vi_text: str = Field(min_length=1, description="越南文圖中文字")
    zh_tw_translation: str = Field(min_length=1, description="繁體中文逐頁翻譯對照")
    image_path: Path | None = Field(default=None, description="本地圖片路徑")
    sha256: str | None = Field(default=None, description="圖片檔案 SHA-256 雜湊值")


class MediaValidationItem(BaseModel):
    """單張圖片驗證結果明細。"""

    filename: str
    path: str
    index: int
    width: int
    height: int
    mime_type: str
    file_size_bytes: int
    sha256: str


class MediaValidationReport(BaseModel):
    """六張照片輪播品質 Gate 驗證報告。"""

    directory: str
    passed: bool
    expected_count: int = 6
    actual_count: int = 0
    items: list[MediaValidationItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CarouselPostPackage(BaseModel):
    """六頁照片輪播完整貼文資料包（對應 content_posts 核心結構）。"""

    post_id: str = Field(description="貼文唯一識別碼")
    slot: ContentSlot = Field(default=ContentSlot.NOON_1130)
    scheduled_at_taipei: str = Field(description="預定發布時間（Asia/Taipei ISO 字串）")
    vi_title: str = Field(max_length=90, description="越南文標題（TikTok 照片標題上限 90 字元）")
    zh_tw_title: str = Field(description="繁體中文標題翻譯")
    vi_caption: str = Field(max_length=4000, description="越南文貼文內容（不含或含標籤）")
    zh_tw_caption: str = Field(description="繁體中文貼文翻譯")
    hashtags: list[str] = Field(description="恰好五個簡明相關 hashtag")
    slides: list[SlideItem] = Field(description="六頁圖文對照清單")
    media_dir: Path = Field(description="六張獨立 JPEG 所在目錄")
    is_mock: bool = Field(default=True, description="是否為測試/Mock 內容（嚴禁將 Mock 當作正式成品）")
    status: PostStatus = Field(default=PostStatus.DRAFT)

    @field_validator("hashtags")
    @classmethod
    def validate_five_hashtags(cls, v: list[str]) -> list[str]:
        """強制檢查恰好五個簡明相關 hashtag（MASTER_SPEC.md 第 3 節）。"""
        if len(v) != 5:
            raise ValueError(f"必須恰好提供 5 個 hashtag，目前數量為 {len(v)}")
        normalized: list[str] = []
        for tag in v:
            clean = tag.strip()
            if not clean:
                raise ValueError("Hashtag 不得為空白")
            if not clean.startswith("#"):
                clean = f"#{clean}"
            normalized.append(clean)
        return normalized

    @field_validator("slides")
    @classmethod
    def validate_six_slides(cls, v: list[SlideItem]) -> list[SlideItem]:
        """強制檢查恰好六頁且順序為 1..6。"""
        if len(v) != 6:
            raise ValueError(f"必須恰好包含 6 頁輪播資料，目前為 {len(v)} 頁")
        indices = [item.index for item in v]
        if indices != [1, 2, 3, 4, 5, 6]:
            raise ValueError(f"輪播頁碼必須嚴格為 [1, 2, 3, 4, 5, 6]，目前為 {indices}")
        return v


class PublishExecutionPlan(BaseModel):
    """發布執行計畫與結果報告（支援 dry-run 與真實狀態回報）。"""

    post_id: str
    adapter_name: str
    dry_run: bool
    allow_live_publish: bool
    status: PostStatus
    privacy_level: Literal["PUBLIC_TO_EVERYONE", "SELF_ONLY", "MANUAL_APP", "MOCK_ONLY"]
    media_validation: MediaValidationReport
    endpoint_or_target: str
    payload_preview: dict[str, object] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    executed_at: datetime = Field(default_factory=datetime.now)
