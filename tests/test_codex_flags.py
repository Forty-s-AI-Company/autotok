"""所有呼叫 `codex exec` 的地方，使用的旗標都必須是本機 codex 實際支援的（避免再出現 unexpected argument）。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.config import AppSettings
from app.engines import find_codex_bin

CODEX = find_codex_bin()
pytestmark = pytest.mark.skipif(CODEX is None, reason="本機沒有 Codex CLI")


def exec_flags() -> set[str]:
    out = subprocess.run([str(CODEX), "exec", "--help"], capture_output=True, text=True, timeout=30, check=False)
    out = out.stdout + out.stderr
    return set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*|-[a-zA-Z])\b", out))


# 先取得實際支援的旗標；後面的測試會把 subprocess.run 換成假的
SUPPORTED = exec_flags() if CODEX is not None else set()


def assert_supported(cmd: list[str]) -> None:
    assert cmd[1] == "exec"
    supported = SUPPORTED
    used = {a for a in cmd[2:-1] if re.fullmatch(r"--?[A-Za-z][A-Za-z0-9-]*", a)}
    assert used <= supported, f"codex exec 不支援：{sorted(used - supported)}"


def test_research_command_flags_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.markets import profile_from_template
    from app.research_providers import LlmWebSearchProvider

    seen: list[list[str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        Path(cmd[cmd.index("-o") + 1]).write_text('{"cases": []}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    p = LlmWebSearchProvider(AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"), profile_from_template("vi-tw"), codex_runner=runner)
    try:
        p.collect_cases()
    except Exception:  # noqa: BLE001  空結果會丟例外，這裡只關心指令
        pass
    assert seen and CODEX is not None
    assert_supported(seen[0])


def test_editorial_command_flags_supported(tmp_path: Path) -> None:
    from app.editorial import CodexCliEditorialProvider

    seen: list[list[str]] = []
    prov = CodexCliEditorialProvider(runner=lambda cmd: (seen.append(cmd), subprocess.CompletedProcess(cmd, 0, "{}", ""))[1])
    prov.complete("hello")
    assert_supported(seen[0])


def test_image_generation_command_flags_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import ContentSlot
    from app.production import ChatGPTCodexCliCarouselGenerator

    seen: list[list[str]] = []

    def fake_run(cmd, **_):  # noqa: ANN001, ANN003
        seen.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr("app.production.subprocess.run", fake_run)
    gen = ChatGPTCodexCliCarouselGenerator()
    with pytest.raises(Exception):  # noqa: B017
        gen.generate_slot_via_codex(
            slot=ContentSlot.NOON_1130, vi_out_dir=tmp_path / "v", zh_out_dir=tmp_path / "z", workspace_dir=tmp_path,
            raw_slides_override=[("a", "b", "c：d")] * 3,
        )
    assert_supported(seen[0])


def test_ping_command_flags_supported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.web.main import create_app

    seen: list[list[str]] = []

    def fake_run(cmd, **_):  # noqa: ANN001, ANN003
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "OK", "")

    monkeypatch.setattr("app.web.routes.engines.subprocess.run", fake_run)
    app = create_app(tmp_path, AppSettings(SQLITE_DB_PATH=tmp_path / "t.db"), allowed_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-AutoTok-Token": app.state.csrf_token})
    c.post("/engines/ping")
    for _ in range(100):
        if app.state.jobs.current() is None:
            break
        import time

        time.sleep(0.05)
    assert seen
    assert_supported(seen[0])
