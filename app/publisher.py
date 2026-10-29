"""單篇貼文的發布流程。介面固定：`publish_post(...)`，預期內的狀況以 `PublishOutcome` 表示，不用丟例外。

流程：匯出（沒有授權或能力為 export_only）或呼叫 TikTok 官方 Content Posting API（DIRECT_POST、PHOTO、PULL_FROM_URL）。
真實發送受閘門保護：ALLOW_LIVE_PUBLISH、非模擬貼文、品質檢查、圖片託管、發布鎖，任何一項不成立都回 blocked。
尚未用真實 TikTok 帳號驗證，見 docs/TIKTOK_PUBLISHING.md。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app import credentials
from app.adapters import get_uploader_adapter
from app.config import AppSettings
from app.errors import AutotokSystemError, MediaValidationError
from app.integrations.tiktok import TikTokClient, TikTokTokens
from app.media import verify_carousel_directory
from app.models import CarouselPostPackage, PostStatus
from app.state import PostStateRepository

TITLE_LIMIT = 90
DESCRIPTION_LIMIT = 4000
POLL_DELAYS = (3, 5, 10, 15, 20, 30, 30, 30, 30, 30)  # 秒；總計約 3 分鐘，狀態端點每分鐘上限 30 次
LOCKED_STATUSES = {PostStatus.PUBLISHED, PostStatus.SENT_TO_INBOX, PostStatus.PUBLISHING, PostStatus.UNKNOWN}


@dataclass(frozen=True)
class PublishOutcome:
    # exported 已匯出發布包｜published 已公開發布｜private 已發布為僅自己可見｜inbox 已傳到 TikTok 收件匣草稿｜blocked 被閘門擋下｜failed 失敗
    status: str
    detail: str
    export_dir: str = ""
    platform_post_id: str = ""


# ---------------------------------------------------------------------------
# 圖片託管
# ---------------------------------------------------------------------------


class ImageHost(ABC):
    """把本機圖片變成 TikTok 可以拉取的公開 HTTPS 網址。"""

    @abstractmethod
    def upload(self, post_id: str, files: list[Path]) -> list[str]: ...

    @abstractmethod
    def cleanup(self, post_id: str) -> None: ...


class SyncDirImageHost(ImageHost):
    """複製到本機同步資料夾；使用者負責把該資料夾發佈到已驗證網域（例如靜態站 repo 的工作目錄）。"""

    def __init__(self, sync_dir: Path, url_prefix: str) -> None:
        self.sync_dir = sync_dir
        self.url_prefix = url_prefix.rstrip("/")

    def upload(self, post_id: str, files: list[Path]) -> list[str]:
        target = self.sync_dir / post_id
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        urls = []
        for f in files:
            shutil.copy2(f, target / f.name)
            urls.append(f"{self.url_prefix}/{post_id}/{f.name}")
        return urls

    def cleanup(self, post_id: str) -> None:
        shutil.rmtree(self.sync_dir / post_id, ignore_errors=True)


GitRunner = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120
    )


class GitPushImageHost(SyncDirImageHost):
    """同步資料夾位在靜態站 git repo 內：複製 → commit → push → 輪詢到圖片網址真的上線才回傳。

    TikTok 的 PULL_FROM_URL 在 init 當下就會去抓圖，所以一定要等部署完成。網址必須回 200、
    內容類型是 image/*、大小與本機檔案一致（靜態站找不到檔案時常回 200 的錯誤頁，不能只看狀態碼）。
    只 commit 這篇貼文的資料夾（pathspec），不會帶到工作目錄的其他改動。
    """

    def __init__(
        self,
        sync_dir: Path,
        url_prefix: str,
        *,
        branch: str = "main",
        wait_sec: float = 300,
        poll_sec: float = 10,
        git: GitRunner = _run_git,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(sync_dir, url_prefix)
        self.branch = branch
        self.wait_sec = wait_sec
        self.poll_sec = poll_sec
        self._git = git
        self._repo_root: Path | None = None
        self._transport = transport
        self._sleep = sleep
        self._clock = clock

    def _root(self) -> Path:
        if self._repo_root is None:
            self.sync_dir.mkdir(parents=True, exist_ok=True)
            proc = self._git(["rev-parse", "--show-toplevel"], self.sync_dir)
            if proc.returncode != 0 or not (proc.stdout or "").strip():
                detail = (proc.stderr or proc.stdout or "").strip()[-300:]
                raise AutotokSystemError(f"圖片託管資料夾不在 git repo 內：{detail}", code="IMAGE_HOST_GIT")
            self._repo_root = Path(proc.stdout.strip()).resolve()
        return self._repo_root

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        # 一律在 repo 根目錄執行，pathspec 才會以根目錄為準
        return self._git(list(args), self._root())

    def _ok(self, *args: str, hint: str = "") -> str:
        proc = self._run(*args)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-300:]
            raise AutotokSystemError(f"圖片託管的 git {args[0]} 失敗：{detail}{hint}", code="IMAGE_HOST_GIT")
        return (proc.stdout or "").strip()

    def _publish_paths(self, rel: str, message: str) -> None:
        self._ok("add", "-A", "--", rel)
        if not self._ok("status", "--porcelain", "--", rel):
            return  # 沒有變更（例如內容與上次相同）
        self._ok("commit", "-m", message, "--", rel)
        self._ok("push", "origin", self.branch, hint="（遠端可能有新的 commit，請先在專案資料夾 git pull 再重試）")

    def _rel(self, post_id: str) -> str:
        return (self.sync_dir / post_id).resolve().relative_to(self._root()).as_posix()

    def upload(self, post_id: str, files: list[Path]) -> list[str]:
        current = self._ok("rev-parse", "--abbrev-ref", "HEAD")
        if current != self.branch:
            raise AutotokSystemError(
                f"圖片託管需要在 {self.branch} 分支上推送（目前在 {current}），否則網站不會部署到正式網域", code="IMAGE_HOST_GIT"
            )
        urls = super().upload(post_id, files)
        self._publish_paths(self._rel(post_id), f"chore(media): 發布暫存圖片 {post_id}")
        self._wait_until_live(urls, [f.stat().st_size for f in files])
        return urls

    def _wait_until_live(self, urls: list[str], sizes: list[int]) -> None:
        deadline = self._clock() + self.wait_sec
        pending = list(zip(urls, sizes, strict=True))
        with httpx.Client(transport=self._transport, timeout=20, follow_redirects=True) as client:
            while True:
                still = []
                for url, size in pending:
                    try:
                        r = client.get(url)
                        live = r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) == size
                    except httpx.HTTPError:
                        live = False
                    if not live:
                        still.append((url, size))
                pending = still
                if not pending:
                    return
                if self._clock() >= deadline:
                    names = ", ".join(u.rsplit("/", 1)[-1] for u, _ in pending)
                    raise AutotokSystemError(
                        f"圖片推送後 {int(self.wait_sec)} 秒內仍打不開（{names}）。請檢查網站部署狀態；沒有通知 TikTok。",
                        code="IMAGE_HOST_TIMEOUT",
                    )
                self._sleep(self.poll_sec)

    def cleanup(self, post_id: str) -> None:
        """刪除圖片並再推一次。盡力而為：失敗只留下圖片，不能影響已經完成的發布結果，所以絕不丟例外。"""
        try:
            super().cleanup(post_id)
            self._publish_paths(self._rel(post_id), f"chore(media): 移除暫存圖片 {post_id}")
        except Exception:  # noqa: BLE001
            return


def image_host_from_settings(settings: AppSettings) -> ImageHost | None:
    prefix = settings.tiktok_verified_image_url_prefix.strip()
    folder = settings.image_host_sync_dir.strip()
    if not prefix or not folder or not prefix.startswith("https://"):
        return None
    if settings.image_host_git_push:
        return GitPushImageHost(Path(folder).expanduser(), prefix, branch=settings.image_host_git_branch.strip() or "main")
    return SyncDirImageHost(Path(folder).expanduser(), prefix)


# ---------------------------------------------------------------------------
# 發布
# ---------------------------------------------------------------------------


def build_payload(pkg: CarouselPostPackage, urls: list[str], privacy_level: str) -> dict[str, object]:
    description = f"{pkg.vi_caption}\n\n{' '.join(pkg.hashtags)}"
    return {
        "media_type": "PHOTO",
        "post_mode": "DIRECT_POST",
        "post_info": {
            "title": pkg.vi_title[:TITLE_LIMIT],
            "description": description[:DESCRIPTION_LIMIT],
            "privacy_level": privacy_level,
            "disable_comment": False,
            "auto_add_music": True,
            "is_aigc": True,  # 圖片由 AI 生成，依 TikTok 規範標示
        },
        "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
    }


def build_inbox_payload(pkg: CarouselPostPackage, urls: list[str]) -> dict[str, object]:
    """傳到收件匣（MEDIA_UPLOAD）：只帶標題與說明；隱私、留言、音樂、AI 生成標示都由使用者在 TikTok App 內自己選。"""
    description = f"{pkg.vi_caption}\n\n{' '.join(pkg.hashtags)}"
    return {
        "media_type": "PHOTO",
        "post_mode": "MEDIA_UPLOAD",
        "post_info": {"title": pkg.vi_title[:TITLE_LIMIT], "description": description[:DESCRIPTION_LIMIT]},
        "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": urls},
    }


INBOX_DONE_STATES = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}
INBOX_HINT = "已傳到 TikTok 收件匣草稿。請到手機 TikTok App 的通知或收件匣完成發布；發布時請自己選隱私，並開啟「AI 生成內容」標示。"


def _export(settings: AppSettings, pkg: CarouselPostPackage, dry_run: bool, reason: str) -> PublishOutcome:
    plan = get_uploader_adapter("manual_export", settings).publish_carousel(pkg, dry_run=dry_run)
    prefix = "（dry-run）會匯出發布包" if dry_run else "已匯出發布包"
    return PublishOutcome("exported", f"{prefix}：{reason}", export_dir=plan.endpoint_or_target)


def _load_tokens(account_id: str, token_ref: str) -> TikTokTokens | None:
    raw = credentials.get_secret(token_ref or f"tiktok:{account_id}")
    return TikTokTokens.loads(raw) if raw else None


def mark_inbox_post_published(
    repo: PostStateRepository,
    settings: AppSettings,
    post_id: str,
    *,
    image_host: ImageHost | None = None,
) -> str:
    """收件匣草稿確認已在 TikTok 發布：狀態改 PUBLISHED（Notion 下次同步跟上），並刪除託管的暫存圖片。

    TikTok 的收件匣模式不會回報使用者是否已發布（實測狀態會一直停在 SEND_TO_USER_INBOX），所以由使用者在控制台按「標記為已發布」呼叫這裡。
    """
    if repo.get_post_status(post_id) != PostStatus.SENT_TO_INBOX:
        raise AutotokSystemError("這篇不是「已傳到收件匣」的草稿，不能標記為已發布", code="NOT_INBOX_DRAFT")
    repo.update_status(post_id, PostStatus.PUBLISHED, last_error=None)
    host = image_host or image_host_from_settings(settings)
    if host is not None:
        host.cleanup(post_id)  # 已發布，暫存圖片可以刪了
    return "已標記為已發布"


def publish_post(
    repo: PostStateRepository,
    settings: AppSettings,
    account_id: str,
    post_id: str,
    *,
    dry_run: bool = False,
    transport: httpx.BaseTransport | None = None,
    image_host: ImageHost | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PublishOutcome:
    pkg = next((p for p in repo.list_posts(account_id=account_id) if p.post_id == post_id), None)
    if pkg is None:
        return PublishOutcome("failed", f"找不到貼文 {post_id}")

    binding = repo.get_tiktok_binding(account_id)
    cap = str(binding["publish_capability"]) if binding else "export_only"
    tokens = _load_tokens(account_id, str(binding["token_ref"]) if binding else "")
    if cap not in ("public", "self_only") or tokens is None:
        return _export(settings, pkg, dry_run, "沒有可用的 TikTok 發布授權")

    # --- 閘門：任何一項不成立都回 blocked，不碰網路 ---
    if not settings.allow_live_publish:
        return PublishOutcome("blocked", "ALLOW_LIVE_PUBLISH 未開啟")
    if pkg.is_mock:
        return PublishOutcome("blocked", "這是模擬貼文，不能發布到正式帳號")
    try:
        report = verify_carousel_directory(pkg.media_dir, raise_on_error=True)
    except MediaValidationError as exc:
        return PublishOutcome("blocked", f"品質檢查未通過：{exc}")
    status = repo.get_post_status(post_id)
    if status in LOCKED_STATUSES:
        return PublishOutcome("blocked", f"貼文狀態為 {status.value if status else ''}，不重複發布；請先確認 TikTok 上是否已有這篇")
    host = image_host or image_host_from_settings(settings)
    if host is None:
        return PublishOutcome("blocked", "尚未設定圖片託管（TIKTOK_VERIFIED_IMAGE_URL_PREFIX 與 IMAGE_HOST_SYNC_DIR）")

    # 未通過審核的 App 一律只能 SELF_ONLY
    want = "PUBLIC_TO_EVERYONE" if (cap == "public" and settings.tiktok_app_audited) else "SELF_ONLY"
    inbox = settings.tiktok_post_mode == "inbox"  # 傳到收件匣草稿，由使用者在 App 內按下發布
    if dry_run:
        urls = [f"(託管後網址)/{post_id}/{i.filename}" for i in report.items]
        if inbox:
            return PublishOutcome("exported", f"（dry-run）會把 {len(urls)} 張傳到 TikTok 收件匣草稿，欄位：{sorted(build_inbox_payload(pkg, urls)['post_info'])}")  # type: ignore[arg-type]
        payload = build_payload(pkg, urls, want)
        return PublishOutcome("exported", f"（dry-run）會以 {want} 發布 {len(urls)} 張，欄位：{sorted(payload['post_info'])}")  # type: ignore[arg-type]

    client = TikTokClient(settings, transport=transport)
    try:
        if tokens.expires_at - time.time() < 300 and tokens.refresh_token:
            tokens = client.refresh(tokens.refresh_token)
            credentials.set_secret(f"tiktok:{account_id}", tokens.dumps())  # refresh token 已旋轉，立刻存回
        info = {} if inbox else client.creator_info(tokens.access_token)  # 收件匣模式的隱私由使用者在 App 內選，不需檢查
    except AutotokSystemError as exc:
        return PublishOutcome("failed", f"發布前檢查失敗：{exc}")
    if not inbox:
        options = [str(o) for o in info.get("privacy_level_options", []) if isinstance(o, str)]  # type: ignore[union-attr]
        if want not in options:
            return PublishOutcome("blocked", f"TikTok 目前不提供 {want}（可用：{', '.join(options) or '無'}）")

    runner_id = f"publisher-{os.getpid()}"
    if not repo.acquire_publish_lock(post_id, runner_id):
        return PublishOutcome("blocked", "發布鎖已被其他執行器持有，不重複發布")
    repo.update_status(post_id, PostStatus.PUBLISHING)
    sent = False
    try:
        urls = host.upload(post_id, [Path(i.path) for i in report.items])
        payload = build_inbox_payload(pkg, urls) if inbox else build_payload(pkg, urls, want)
        publish_id = ""
        try:
            sent = True  # 從這裡開始，請求可能已送達 TikTok
            publish_id = client.init_photo_post(tokens.access_token, payload)
        except AutotokSystemError as exc:
            if exc.code != "TIKTOK_NETWORK":  # TikTok 有回應並明確拒絕，沒有建立貼文
                repo.update_status(post_id, PostStatus.FAILED, last_error=str(exc))
                host.cleanup(post_id)
                return PublishOutcome("failed", f"TikTok 拒絕發布：{exc}")
            repo.update_status(post_id, PostStatus.UNKNOWN, last_error=str(exc))
            return PublishOutcome("failed", f"送出後連線中斷，結果不明，已標為 UNKNOWN 不會重傳：{exc}")

        fail_reason = ""
        post_ids: list[str] = []
        for delay in POLL_DELAYS:
            sleep(delay)
            try:
                st = client.fetch_status(tokens.access_token, publish_id)
            except AutotokSystemError:
                continue
            state = str(st.get("status", ""))
            if state in (INBOX_DONE_STATES if inbox else {"PUBLISH_COMPLETE"}):
                post_ids = [str(x) for x in st.get("publicaly_available_post_id", []) if x]  # type: ignore[union-attr]
                break
            if state == "FAILED":
                fail_reason = str(st.get("fail_reason", "unknown"))
                break
        else:
            repo.update_status(post_id, PostStatus.UNKNOWN, last_error=f"輪詢逾時，publish_id={publish_id}")
            return PublishOutcome("failed", f"輪詢逾時，結果不明，已標為 UNKNOWN 不會重傳（publish_id={publish_id}）", platform_post_id=publish_id)

        if fail_reason:
            repo.update_status(post_id, PostStatus.FAILED, last_error=f"TikTok 處理失敗：{fail_reason}")
            host.cleanup(post_id)
            return PublishOutcome("failed", f"TikTok 處理失敗：{fail_reason}", platform_post_id=publish_id)

        pid = post_ids[0] if post_ids else publish_id
        if inbox:
            repo.update_status(post_id, PostStatus.SENT_TO_INBOX, platform_post_id=pid, last_error=None)
            # 圖片不能在這裡刪：使用者在 TikTok 編輯草稿時可能還會讀取原圖（提早刪除會讓編輯器全黑）。
            # 等使用者標記已發布後才由 mark_inbox_post_published 清理。
            return PublishOutcome("inbox", INBOX_HINT, platform_post_id=pid)
        repo.update_status(post_id, PostStatus.PUBLISHED, platform_post_id=pid, last_error=None)
        host.cleanup(post_id)
        if want == "SELF_ONLY":
            return PublishOutcome("private", "已發布為僅自己可見", platform_post_id=pid)
        return PublishOutcome("published", "已公開發布", platform_post_id=pid)
    except Exception as exc:  # noqa: BLE001
        repo.update_status(post_id, PostStatus.UNKNOWN if sent else PostStatus.FAILED, last_error=str(exc))
        return PublishOutcome("failed", f"發布過程出錯：{exc}")
    finally:
        repo.release_publish_lock(post_id)
