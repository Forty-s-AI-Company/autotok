"""生圖與文案引擎：模型選擇、啟動時掃描、手動測試連線。"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.engines import EngineError, ModelInfo, codex_status, find_codex_bin, list_codex_models, scan_codex
from app.web.jobs import JobBusyError, JobHandle
from app.web.ui import templates, toast

router = APIRouter()
MODEL_SETTING_KEY = "codex_model"
CACHE_SECONDS = 600


def apply_saved_model(app) -> None:  # noqa: ANN001
    """啟動時把使用者上次選的模型套用到設定。"""
    saved = app.state.repo.get_app_setting(MODEL_SETTING_KEY)
    if saved:
        app.state.settings.codex_model = saved


def run_scan(app) -> tuple[str, str]:  # noqa: ANN001
    """掃描並寫入 sync_log（job=engine_check）。回傳 (level, message)。"""
    repo, cfg = app.state.repo, app.state.settings
    log_id = repo.log_job_start(account_id="", job="engine_check", label="引擎掃描")
    result = scan_codex(cfg.codex_bin, cfg.codex_model)
    app.state.model_cache = (datetime.now(UTC).timestamp(), list(result.models))
    status = {"ok": "ok", "warn": "ok", "err": "error"}[result.level]
    repo.log_job_finish(log_id, status=status, summary=f"{result.level}|{result.message}" if status == "ok" else "", error=result.message if status == "error" else "")
    return result.level, result.message


def start_background_scan(app) -> None:  # noqa: ANN001
    threading.Thread(target=run_scan, args=(app,), name="engine-scan", daemon=True).start()


def last_scan(repo) -> dict[str, object] | None:  # noqa: ANN001
    row = repo.last_job(account_id="", job="engine_check")
    if row is None:
        return None
    if row["status"] == "ok":
        level, _, msg = str(row["summary"]).partition("|")
    elif row["status"] == "running":
        level, msg = "run", "掃描中…"
    else:
        level, msg = "err", str(row["error"])
    return {"level": level or "ok", "message": msg, "finished_at": row["finished_at"], "running": row["status"] == "running"}


def _models(app, *, force: bool = False) -> tuple[list[ModelInfo], str]:  # noqa: ANN001
    cached = getattr(app.state, "model_cache", None)
    now = datetime.now(UTC).timestamp()
    if cached and not force and now - cached[0] < CACHE_SECONDS and cached[1]:
        return cached[1], ""
    try:
        models = list_codex_models(app.state.settings.codex_bin)
    except EngineError as exc:
        return (cached[1] if cached else []), str(exc)
    app.state.model_cache = (now, models)
    return models, ""


@router.get("/engines/panel", response_class=HTMLResponse)
def panel(request: Request) -> HTMLResponse:
    app = request.app
    cfg = app.state.settings
    repo = app.state.repo
    models, err = _models(app)
    active = repo.get_active_account()
    return templates.TemplateResponse(
        request,
        "_engines_panel.html",
        {
            "request": request,
            "tz": repo.get_market_profile(active.account_id).timezone,
            "engine": codex_status(cfg.codex_bin),
            "model": cfg.codex_model,
            "models": [m for m in models if m.listed or m.slug == cfg.codex_model],
            "model_in_list": any(m.slug == cfg.codex_model for m in models),
            "list_error": err,
            "scan": last_scan(repo),
            "ping": repo.last_job(account_id="", job="engine_ping"),
        },
    )


@router.post("/engines/scan")
def scan(request: Request) -> Response:
    level, msg = run_scan(request.app)
    return toast(Response(status_code=200), msg, "err" if level == "err" else "ok", events=["enginesChanged"])


@router.post("/engines/model")
def set_model(request: Request, model: str = Form(...)) -> Response:
    app = request.app
    models, _ = _models(app, force=True)
    if models and model not in {m.slug for m in models}:
        return toast(Response(status_code=200), "這個模型不在 Codex 的可用清單中", "err")
    app.state.repo.set_app_setting(MODEL_SETTING_KEY, model)
    app.state.settings.codex_model = model
    level, msg = run_scan(app)
    return toast(Response(status_code=200), f"已改用 {model}。{msg}", "err" if level == "err" else "ok", events=["enginesChanged"])


@router.post("/engines/ping")
def ping(request: Request) -> Response:
    """實際呼叫一次模型確認能回應（會消耗極少量額度）。"""
    app = request.app
    cfg = app.state.settings
    path = find_codex_bin(cfg.codex_bin)
    if path is None:
        return toast(Response(status_code=200), "找不到 Codex CLI", "err")

    def run(handle: JobHandle) -> str:
        handle.progress(f"呼叫 {cfg.codex_model}")
        with tempfile.TemporaryDirectory(prefix="autotok-ping-") as tmp:
            cmd = [str(path), "exec", "--skip-git-repo-check", "--ephemeral", "-m", cfg.codex_model, "-C", tmp, "-s", "read-only", "Reply with the single word OK."]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=120)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("測試逾時（2 分鐘）") from exc
        if proc.returncode != 0:
            raise RuntimeError(f"模型沒有回應（exit={proc.returncode}）：{(proc.stderr or proc.stdout)[-300:]}")
        return f"{cfg.codex_model} 有回應"

    try:
        app.state.jobs.submit(account_id="", job="engine_ping", label=f"測試 {cfg.codex_model}", fn=run)
    except JobBusyError as exc:
        return toast(Response(status_code=200), str(exc), "err")
    return toast(Response(status_code=200), "已開始測試，完成後會顯示結果", events=["enginesChanged"])
