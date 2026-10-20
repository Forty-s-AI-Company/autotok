"""生圖／文案引擎偵測：找出 Codex CLI 的實際路徑，並回報可讀的健康狀態。"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

_MAC_APP_PATH = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def find_codex_bin(explicit: str = "") -> Path | None:
    """依序檢查：設定值 → 環境變數 CODEX_BIN → PATH 中的 codex → macOS ChatGPT App。"""
    candidates: list[Path] = []
    for raw in (explicit, os.environ.get("CODEX_BIN", "")):
        if raw.strip():
            candidates.append(Path(raw.strip()).expanduser())
    on_path = shutil.which("codex")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(_MAC_APP_PATH)
    for path in candidates:
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class EngineStatus:
    ok: bool
    detail: str
    path: str = ""


def codex_status(explicit: str = "") -> EngineStatus:
    path = find_codex_bin(explicit)
    if path is None:
        return EngineStatus(
            ok=False,
            detail="找不到 Codex CLI。請安裝 ChatGPT App，或在 .env 設定 CODEX_BIN 指向 codex 執行檔。",
        )
    return EngineStatus(ok=True, detail="已偵測到 Codex CLI", path=str(path))


# ---------------------------------------------------------------------------
# 模型清單與健康檢查
# ---------------------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402
from collections.abc import Callable  # noqa: E402


@dataclass(frozen=True)
class ModelInfo:
    slug: str
    name: str
    description: str
    listed: bool  # Codex 的 /model 清單是否會顯示它


class EngineError(RuntimeError):
    """讀不到模型清單或 Codex 無法執行。"""


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)


def parse_model_catalog(raw: str) -> list[ModelInfo]:
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise EngineError("Codex 回傳的模型清單不是有效的 JSON") from exc
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        raise EngineError("Codex 回傳的模型清單格式不符")
    out: list[ModelInfo] = []
    for m in sorted((m for m in models if isinstance(m, dict) and m.get("slug")), key=lambda m: m.get("priority", 999)):
        out.append(
            ModelInfo(
                slug=str(m["slug"]),
                name=str(m.get("display_name") or m["slug"]),
                description=str(m.get("description") or ""),
                listed=m.get("visibility") == "list",
            )
        )
    return out


def list_codex_models(explicit_bin: str = "", *, runner: Runner | None = None) -> list[ModelInfo]:
    """用 `codex debug models` 取得與 Codex 內 /model 相同的模型清單。"""
    path = find_codex_bin(explicit_bin)
    if path is None:
        raise EngineError("找不到 Codex CLI")
    try:
        proc = (runner or _default_runner)([str(path), "debug", "models"])
    except subprocess.TimeoutExpired as exc:
        raise EngineError("讀取模型清單逾時") from exc
    if proc.returncode != 0:
        raise EngineError(f"讀取模型清單失敗（exit={proc.returncode}）：{(proc.stderr or proc.stdout)[-200:]}")
    return parse_model_catalog(proc.stdout)


@dataclass(frozen=True)
class ScanResult:
    level: str  # ok | warn | err
    message: str
    models: tuple[ModelInfo, ...] = ()


def scan_codex(explicit_bin: str, model: str, *, runner: Runner | None = None) -> ScanResult:
    """確認 Codex 可執行，且目前選用的模型仍在可用清單中（不會呼叫模型，不消耗額度）。"""
    if find_codex_bin(explicit_bin) is None:
        return ScanResult("err", codex_status(explicit_bin).detail)
    try:
        models = tuple(list_codex_models(explicit_bin, runner=runner))
    except EngineError as exc:
        return ScanResult("warn", f"無法取得模型清單，不能確認「{model}」是否有效：{exc}")
    found = next((m for m in models if m.slug == model), None)
    if found is None:
        return ScanResult("err", f"模型「{model}」已不在 Codex 的可用清單中，請在下方改選其他模型。", models)
    if not found.listed:
        return ScanResult("warn", f"模型「{model}」存在，但 Codex 沒有把它列在一般清單中，可能無法使用。", models)
    return ScanResult("ok", f"模型「{found.name}」在可用清單中", models)
