"""GitPushImageHost：複製 → commit → push → 等圖片網址真的上線。用假的 git 與模擬網站，不碰網路。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from app.config import AppSettings
from app.errors import AutotokSystemError
from app.publisher import GitPushImageHost, SyncDirImageHost, image_host_from_settings

PREFIX = "https://site.example.com/media"
PID = "post-1"


class FakeGit:
    """記錄指令，並依指令回傳預設結果。"""

    def __init__(self, root: Path, *, branch: str = "main", dirty: bool = True, fail: str = "") -> None:
        self.root, self.branch, self.dirty, self.fail = root, branch, dirty, fail
        self.calls: list[list[str]] = []
        self.cwds: list[Path] = []

    def __call__(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        self.cwds.append(cwd)
        out, rc = "", 0
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            out = str(self.root)
        elif args[:2] == ["rev-parse", "--abbrev-ref"]:
            out = self.branch
        elif args[0] == "status":
            out = " A x" if self.dirty else ""
        if self.fail and args[0] == self.fail:
            rc = 1
        return subprocess.CompletedProcess(["git", *args], rc, out, "boom" if rc else "")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


def _files(tmp_path: Path) -> list[Path]:
    src = tmp_path / "src"
    src.mkdir()
    out = []
    for i in (1, 2):
        f = src / f"slide_0{i}.jpg"
        f.write_bytes(b"jpeg-bytes-" * i)
        out.append(f)
    return out


def _site(files: list[Path], *, ready_after: int = 0, wrong_type: bool = False) -> tuple[httpx.MockTransport, list[int]]:
    """第 ready_after 輪之後才回真的圖片；之前回網站的 200 JSON 錯誤頁（真實部署中常見）。"""
    hits = [0]
    by_name = {f.name: f.read_bytes() for f in files}

    def handler(req: httpx.Request) -> httpx.Response:
        hits[0] += 1
        name = req.url.path.rsplit("/", 1)[-1]
        rnd = (hits[0] - 1) // len(files)
        if rnd < ready_after:
            return httpx.Response(200, json={"status": "ok"})
        if wrong_type:
            return httpx.Response(200, headers={"content-type": "application/json"}, content=by_name[name])
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=by_name[name])

    return httpx.MockTransport(handler), hits


def _host(tmp_path: Path, git: FakeGit, transport: httpx.MockTransport, sleeps: list[float], **kw) -> GitPushImageHost:
    clock = {"t": 0.0}

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock["t"] += s

    return GitPushImageHost(
        tmp_path / "repo" / "public" / "media", PREFIX, git=git, transport=transport, sleep=sleep, clock=lambda: clock["t"], **kw
    )


def test_upload_commits_pushes_and_waits_until_live(tmp_path: Path) -> None:
    files = _files(tmp_path)
    git = FakeGit(tmp_path / "repo")
    transport, _ = _site(files, ready_after=2)
    sleeps: list[float] = []
    urls = _host(tmp_path, git, transport, sleeps).upload(PID, files)
    assert urls == [f"{PREFIX}/{PID}/slide_01.jpg", f"{PREFIX}/{PID}/slide_02.jpg"]
    assert git.verbs().index("add") < git.verbs().index("commit") < git.verbs().index("push")
    commit = next(c for c in git.calls if c[0] == "commit")
    assert commit[-2:] == ["--", f"public/media/{PID}"]  # 只 commit 這篇的資料夾
    assert ["push", "origin", "main"] in git.calls
    assert len(sleeps) == 2  # 前兩輪回錯誤頁，所以等了兩次
    # 除了第一次找 repo 根目錄外，git 一律在 repo 根目錄執行（pathspec 以根目錄為準）
    assert all(cwd == (tmp_path / "repo").resolve() for c, cwd in zip(git.calls[1:], git.cwds[1:], strict=True))
    assert (tmp_path / "repo/public/media" / PID / "slide_01.jpg").is_file()


def test_json_200_error_page_is_not_treated_as_live(tmp_path: Path) -> None:
    files = _files(tmp_path)
    transport, _ = _site(files, wrong_type=True)
    host = _host(tmp_path, FakeGit(tmp_path / "repo"), transport, [], wait_sec=30, poll_sec=10)
    with pytest.raises(AutotokSystemError) as exc:
        host.upload(PID, files)
    assert exc.value.code == "IMAGE_HOST_TIMEOUT" and "slide_01.jpg" in str(exc.value)


def test_timeout_when_never_live(tmp_path: Path) -> None:
    files = _files(tmp_path)
    transport, _ = _site(files, ready_after=999)
    sleeps: list[float] = []
    with pytest.raises(AutotokSystemError) as exc:
        _host(tmp_path, FakeGit(tmp_path / "repo"), transport, sleeps, wait_sec=25, poll_sec=10).upload(PID, files)
    assert exc.value.code == "IMAGE_HOST_TIMEOUT" and "沒有通知 TikTok" in str(exc.value)
    assert sum(sleeps) >= 25


def test_wrong_branch_is_refused_before_copying(tmp_path: Path) -> None:
    files = _files(tmp_path)
    git = FakeGit(tmp_path / "repo", branch="feature/x")
    host = _host(tmp_path, git, _site(files)[0], [])
    with pytest.raises(AutotokSystemError) as exc:
        host.upload(PID, files)
    assert "feature/x" in str(exc.value)
    assert "push" not in git.verbs() and not (tmp_path / "repo/public/media" / PID).exists()


def test_push_failure_raises_and_never_polls(tmp_path: Path) -> None:
    files = _files(tmp_path)
    transport, hits = _site(files)
    host = _host(tmp_path, FakeGit(tmp_path / "repo", fail="push"), transport, [])
    with pytest.raises(AutotokSystemError) as exc:
        host.upload(PID, files)
    assert exc.value.code == "IMAGE_HOST_GIT" and "git pull" in str(exc.value)
    assert hits[0] == 0


def test_unchanged_content_skips_commit_but_still_waits(tmp_path: Path) -> None:
    files = _files(tmp_path)
    git = FakeGit(tmp_path / "repo", dirty=False)
    _host(tmp_path, git, _site(files)[0], []).upload(PID, files)
    assert "commit" not in git.verbs() and "push" not in git.verbs()


def test_cleanup_removes_and_pushes_deletion(tmp_path: Path) -> None:
    files = _files(tmp_path)
    git = FakeGit(tmp_path / "repo")
    host = _host(tmp_path, git, _site(files)[0], [])
    host.upload(PID, files)
    git.calls.clear()
    host.cleanup(PID)
    assert not (tmp_path / "repo/public/media" / PID).exists()
    commit = next(c for c in git.calls if c[0] == "commit")
    assert "移除暫存圖片" in commit[2] and ["push", "origin", "main"] in git.calls


def test_cleanup_never_raises_even_if_git_fails(tmp_path: Path) -> None:
    files = _files(tmp_path)
    git = FakeGit(tmp_path / "repo")
    host = _host(tmp_path, git, _site(files)[0], [])
    host.upload(PID, files)
    git.fail = "push"
    host.cleanup(PID)  # 不能影響已完成的發布結果
    assert not (tmp_path / "repo/public/media" / PID).exists()


def test_settings_choose_host_type(tmp_path: Path) -> None:
    base = {"TIKTOK_VERIFIED_IMAGE_URL_PREFIX": PREFIX, "IMAGE_HOST_SYNC_DIR": str(tmp_path / "m"), "SQLITE_DB_PATH": tmp_path / "t.db"}
    plain = image_host_from_settings(AppSettings(**base))
    pushing = image_host_from_settings(AppSettings(IMAGE_HOST_GIT_PUSH=True, IMAGE_HOST_GIT_BRANCH="deploy", **base))
    assert type(plain) is SyncDirImageHost
    assert isinstance(pushing, GitPushImageHost) and pushing.branch == "deploy"
