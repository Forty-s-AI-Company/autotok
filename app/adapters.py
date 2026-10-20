"""可替換之六張照片輪播發布器介面（UploaderAdapter）與四種實作。

遵循 MASTER_SPEC.md 第 8、10、11 節與 PHASE1_ANTIGRAVITY_PROMPT.md 第 5、6 點：
1. `MockCarouselAdapter` (`mock`)：本機與 CI 安全模擬驗證，不發出任何外部網路請求。
2. `OfficialTikTokApiAdapter` (`official_api`)：對應 TikTok 官方 Content Posting API
   `POST https://open.tiktokapis.com/v2/post/publish/content/init/` (`media_type="PHOTO"`, `post_mode="DIRECT_POST"` 或 `"MEDIA_UPLOAD"`).
   - 支援 `--dry-run` 輸出完整 API Payload 與安全檢核計畫。
   - 若要求真实公開發布 (`--live`)：
     * 未設定 `ALLOW_LIVE_PUBLISH=true` 或缺少 Token / 已驗證圖片網域前綴 -> raise `BlockedNeedsAuthError` (`BLOCKED_NEEDS_AUTH`)。
     * 未通過 TikTok App Audit (`TIKTOK_APP_AUDITED=false`) 卻要求公開發布 (`PUBLIC_TO_EVERYONE`) -> raise `BlockedNeedsAuditError` (`BLOCKED_NEEDS_AUDIT`)。
3. `AutotokCandidateAdapter` (`autotok_candidate`)：經 Phase 1 實證研究確認，`makiisthenes/TiktokAutoUploader` (v2.1.0 / commit `0f76155`)
   僅支援單一影片上傳（`FileType=video`、`-v/--video`、AGPL-3.0 授權），原生不支援六張獨立 JPEG 照片輪播（Issue #70 作者明確回覆不支援）。
   - 無論 dry-run 或 live，皆明確 raise `UnsupportedFeatureError` 並附帶完整原始碼證據，絕不偽裝成功或偷轉影片。
4. `ManualExportAdapter` (`manual_export`)：當官方 API 尚未完成網域與 App Audit 審核時的合規半自動備案，
   將通過驗證的 6 張獨立 1080×1350 JPEG、越南文標題/內文、5 個 Hashtag 與繁中逐頁對照匯出為可立即於 TikTok 發布的完整內容包。
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod

from app.config import AppSettings
from app.errors import (
    BlockedNeedsAuditError,
    BlockedNeedsAuthError,
    UnsupportedFeatureError,
)
from app.media import verify_carousel_directory
from app.models import (
    CarouselPostPackage,
    PostStatus,
    PublishExecutionPlan,
)


class UploaderAdapter(ABC):
    """六張照片輪播發布器抽象基底類別。"""

    adapter_name: str

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    @abstractmethod
    def publish_carousel(
        self,
        package: CarouselPostPackage,
        *,
        dry_run: bool = True,
        post_mode: str = "DIRECT_POST",
    ) -> PublishExecutionPlan:
        """執行或預覽六張照片輪播發布計畫。"""


class MockCarouselAdapter(UploaderAdapter):
    """安全測試用 Mock 發布器，不進行任何外部呼叫，亦絕不宣稱為真實公開發布。"""

    adapter_name = "mock"

    def publish_carousel(
        self,
        package: CarouselPostPackage,
        *,
        dry_run: bool = True,
        post_mode: str = "DIRECT_POST",
    ) -> PublishExecutionPlan:
        validation = verify_carousel_directory(package.media_dir, raise_on_error=True)
        if not dry_run and not self.settings.allow_live_publish:
            raise BlockedNeedsAuthError(
                "安全攔截：預設禁止真實發布（ALLOW_LIVE_PUBLISH=false）。"
                "請使用 --dry-run，或在獲得授權後明確設定 ALLOW_LIVE_PUBLISH=true。"
            )

        full_caption = f"{package.vi_caption}\n\n{' '.join(package.hashtags)}"
        return PublishExecutionPlan(
            post_id=package.post_id,
            adapter_name=self.adapter_name,
            dry_run=dry_run,
            allow_live_publish=self.settings.allow_live_publish,
            status=PostStatus.READY if dry_run else PostStatus.READY_TO_REVIEW,
            privacy_level="MOCK_ONLY",
            media_validation=validation,
            endpoint_or_target="mock://local/tiktok/carousel/v2/content/init",
            payload_preview={
                "note": "此為本機 Mock 驗證輸出，未與 TikTok 伺服器通訊，不得視為真實公開發布。",
                "post_mode": post_mode,
                "media_type": "PHOTO",
                "title": package.vi_title,
                "description": full_caption,
                "slide_count": validation.actual_count,
                "slides_sha256": [item.sha256 for item in validation.items],
            },
            warnings=[
                "目前使用 mock adapter，所有產物標記為 TEST/MOCK，無任何外部副作用。"
            ],
            next_actions=[
                "若需產生半自動可發布套件，請改用 --adapter manual_export",
                "若需檢視官方 Content Posting API 結構，請改用 --adapter official_api --dry-run",
            ],
        )


class OfficialTikTokApiAdapter(UploaderAdapter):
    """TikTok 官方 Content Posting API 照片輪播發布器 (`POST /v2/post/publish/content/init/`)。"""

    adapter_name = "official_api"
    INIT_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/content/init/"
    STATUS_ENDPOINT = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"

    def publish_carousel(
        self,
        package: CarouselPostPackage,
        *,
        dry_run: bool = True,
        post_mode: str = "DIRECT_POST",
    ) -> PublishExecutionPlan:
        validation = verify_carousel_directory(package.media_dir, raise_on_error=True)

        url_prefix = (
            self.settings.tiktok_verified_image_url_prefix.rstrip("/")
            or "https://verified-domain.example.com/tiktok-media"
        )
        photo_urls = [
            f"{url_prefix}/{package.post_id}/{item.filename}" for item in validation.items
        ]
        full_description = f"{package.vi_caption}\n\n{' '.join(package.hashtags)}"

        privacy_level = (
            "PUBLIC_TO_EVERYONE" if self.settings.tiktok_app_audited else "SELF_ONLY"
        )

        payload: dict[str, object] = {
            "media_type": "PHOTO",
            "post_mode": post_mode,
            "post_info": {
                "title": package.vi_title,
                "description": full_description,
                "privacy_level": privacy_level,
                "disable_comment": False,
                "auto_add_music": True,
            },
            "source_info": {
                "source": "PULL_FROM_URL",
                "photo_cover_index": 0,
                "photo_images": photo_urls,
            },
        }

        warnings: list[str] = []
        if not self.settings.tiktok_access_token:
            warnings.append("尚未設定 TIKTOK_ACCESS_TOKEN（OAuth 2.0 `video.publish` / `video.upload` scope）。")
        if not self.settings.tiktok_verified_image_url_prefix:
            warnings.append(
                "尚未設定 TIKTOK_VERIFIED_IMAGE_URL_PREFIX（TikTok PULL_FROM_URL 強制要求網域通過 Developer Portal 驗證）。"
            )
        if not self.settings.tiktok_app_audited:
            warnings.append(
                "TIKTOK_APP_AUDITED=false：未通過官方 App Audit 前，DIRECT_POST 僅能以 SELF_ONLY（私人可見）發布，"
                "或改用 post_mode='MEDIA_UPLOAD' 送至創作者 TikTok App 草稿匣手動公開。"
            )

        if dry_run:
            return PublishExecutionPlan(
                post_id=package.post_id,
                adapter_name=self.adapter_name,
                dry_run=True,
                allow_live_publish=self.settings.allow_live_publish,
                status=PostStatus.BLOCKED_NEEDS_AUTH
                if not self.settings.tiktok_access_token
                else PostStatus.READY,
                privacy_level=privacy_level,
                media_validation=validation,
                endpoint_or_target=self.INIT_ENDPOINT,
                payload_preview=payload,
                warnings=warnings,
                next_actions=[
                    "1. 於 TikTok Developer Portal 建立 App 並申請 Content Posting API (video.publish / video.upload)",
                    "2. 完成圖片託管網域 (Domain Verification) 驗證以支援 PULL_FROM_URL",
                    "3. 若未通過 App Audit，可先用 MEDIA_UPLOAD 將 6 張圖送入 TikTok App 收件匣通知完成發布",
                ],
            )

        # 非 dry-run 時嚴格執行安全閘門檢查
        if not self.settings.allow_live_publish:
            raise BlockedNeedsAuthError(
                "BLOCKED_NEEDS_AUTH：ALLOW_LIVE_PUBLISH 為 false，禁止對 TikTok 官方 API 發出真實請求。"
            )

        if not self.settings.tiktok_access_token or not self.settings.tiktok_verified_image_url_prefix:
            raise BlockedNeedsAuthError(
                "BLOCKED_NEEDS_AUTH：缺少 TIKTOK_ACCESS_TOKEN 或 TIKTOK_VERIFIED_IMAGE_URL_PREFIX，"
                "無法執行官方 Content Posting API 六張照片輪播上傳。"
            )

        if post_mode == "DIRECT_POST" and not self.settings.tiktok_app_audited:
            raise BlockedNeedsAuditError(
                "BLOCKED_NEEDS_AUDIT：TikTok 應用程式尚未通過官方 Audit（TIKTOK_APP_AUDITED=false），"
                "官方 API 拒絕 PUBLIC_TO_EVERYONE 直接公開發布。請改用 MEDIA_UPLOAD 或 manual_export 半自動流程。"
            )

        if package.is_mock:
            raise BlockedNeedsAuthError(
                "BLOCKED_NEEDS_AUTH：目前資料包標記為 is_mock=True（測試圖片），系統禁止將 Mock 測試圖發布至正式帳號。"
            )

        raise BlockedNeedsAuthError(
            "BLOCKED_NEEDS_AUTH：此 adapter 只負責 payload 與閘門檢查；真實發送由 app.publisher.publish_post 執行"
            "（取每帳號授權、圖片託管、輪詢狀態與防重發鎖）。"
        )


class AutotokCandidateAdapter(UploaderAdapter):
    """`makiisthenes/TiktokAutoUploader` (autotok) 候選評估介面。

    經 Phase 1 原始碼審計確認：
    - 專案授權為 AGPL-3.0
    - CLI (`autotok/cli.py`) 僅接受 `-v / --video` 或 `-yt / --youtube`
    - 上傳核心 (`autotok/uploader.py`) 硬編碼 `FileType=video`、`/api/v1/video/upload/auth/` 與 `video_id`
    - GitHub Issue #70 作者 `makiisthenes` 明確聲明："I have not supported photos in this repository, it only supports videos mp4"
    因此本 Adapter 明確拋出 `UnsupportedFeatureError`，禁止將六張照片轉為影片或偽裝成功。
    """

    adapter_name = "autotok_candidate"

    def publish_carousel(
        self,
        package: CarouselPostPackage,
        *,
        dry_run: bool = True,
        post_mode: str = "DIRECT_POST",
    ) -> PublishExecutionPlan:
        # 先確認媒體本身是否合規，再明確回報通道不支援
        verify_carousel_directory(package.media_dir, raise_on_error=True)
        raise UnsupportedFeatureError(
            "UNSUPPORTED_FEATURE：經審閱 makiisthenes/TiktokAutoUploader (autotok v2.1.0, commit 0f76155) 原始碼與 Issue #70，"
            "該工具僅支援 MP4 影片上傳（autotok/uploader.py 硬編碼 FileType=video 與 video_id），"
            "不支援 TikTok 原生六張獨立 JPEG 照片輪播，且涉及非官方網頁端簽章 (X-Bogus) 與 AGPL-3.0 授權風險。"
            "請改用 official_api 或 manual_export。"
        )


class ManualExportAdapter(UploaderAdapter):
    """合規半自動匯出發布器（當官方 API 尚未通過公開審核時之立即可用方案）。

    將通過品質 Gate 的 6 張獨立 1080×1350 JPEG、越南文標題/內文、5 個 Hashtag 與繁中對照表
    打包輸出至 `artifacts/manual_exports/<post_id>/`，供操作者透過 TikTok Web 或手機 App 直接選取 6 張圖發布。
    """

    adapter_name = "manual_export"

    def publish_carousel(
        self,
        package: CarouselPostPackage,
        *,
        dry_run: bool = True,
        post_mode: str = "DIRECT_POST",
    ) -> PublishExecutionPlan:
        validation = verify_carousel_directory(package.media_dir, raise_on_error=True)
        export_dir = (self.settings.export_base_dir / package.post_id).resolve()

        full_caption = f"{package.vi_caption}\n\n{' '.join(package.hashtags)}"
        manifest = {
            "post_id": package.post_id,
            "account_id": package.account_id,
            "slot": package.slot.value,
            "scheduled_at_taipei": package.scheduled_at_taipei,
            "is_mock": package.is_mock,
            "vi_title": package.vi_title,
            "zh_tw_title": package.zh_tw_title,
            "vi_caption_with_hashtags": full_caption,
            "zh_tw_caption": package.zh_tw_caption,
            "hashtags": package.hashtags,
            "slides": [
                {
                    "index": s.index,
                    "filename": f"slide_{s.index:02d}.jpg",
                    "vi_text": s.vi_text,
                    "zh_tw_translation": s.zh_tw_translation,
                    "sha256": s.sha256,
                }
                for s in package.slides
            ],
        }

        if not dry_run:
            images_out = export_dir / "images"
            shutil.rmtree(images_out, ignore_errors=True)  # 張數變少後不留下舊圖
            images_out.mkdir(parents=True, exist_ok=True)
            for item in validation.items:
                shutil.copy2(item.path, images_out / item.filename)

            (export_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (export_dir / "caption_vi.txt").write_text(
                f"{package.vi_title}\n\n{full_caption}\n", encoding="utf-8"
            )

        return PublishExecutionPlan(
            post_id=package.post_id,
            adapter_name=self.adapter_name,
            dry_run=dry_run,
            allow_live_publish=self.settings.allow_live_publish,
            status=PostStatus.READY_TO_REVIEW,
            privacy_level="MANUAL_APP",
            media_validation=validation,
            endpoint_or_target=str(export_dir),
            payload_preview=manifest,
            warnings=[
                "半自動匯出模式（CONDITIONAL）：不會自動公開發文至 TikTok，需由操作者在 TikTok App / Web 選取匯出之 6 張 JPEG 完成發布。"
            ],
            next_actions=[
                f"1. 檢視匯出目錄：{export_dir}",
                "2. 確認 6 張 1080x1350 JPEG 與越南文逐頁翻譯無誤",
                "3. 上傳至 TikTok 並於確認公開狀態後，執行狀態更新紀錄平台 Post ID",
            ],
        )


def get_uploader_adapter(name: str, settings: AppSettings | None = None) -> UploaderAdapter:
    """依名稱取得對應的 UploaderAdapter 實例。"""
    cfg = settings or AppSettings()
    registry: dict[str, type[UploaderAdapter]] = {
        "mock": MockCarouselAdapter,
        "official_api": OfficialTikTokApiAdapter,
        "autotok_candidate": AutotokCandidateAdapter,
        "manual_export": ManualExportAdapter,
    }
    if name not in registry:
        supported = ", ".join(sorted(registry.keys()))
        raise UnsupportedFeatureError(f"未知的 UploaderAdapter '{name}'，支援的選項為: {supported}")
    return registry[name](cfg)
