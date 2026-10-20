"""FastAPI 應用組裝：安全中介層、靜態檔、共用端點與各頁路由。"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import AppSettings
from app.state import PostStateRepository
from app.web import security
from app.web.jobs import JobRunner
from app.web.routes import (
    analytics,
    engines,
    library,
    notifications,
    notion,
    research,
    schedule,
    settings_page,
    studio,
    tiktok,
    today,
)
from app.web.ui import WEB_DIR, local_dt, templates

ALLOWED_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png"}


def create_app(
    project_root: Path | None = None,
    settings: AppSettings | None = None,
    *,
    allowed_hosts: set[str] | None = None,
    startup_scan: bool = False,
) -> FastAPI:
    root = (project_root or Path.cwd()).resolve()
    cfg = settings or AppSettings()
    repo = PostStateRepository(cfg.sqlite_db_path)
    repo.fail_stale_running_jobs()

    app = FastAPI(title="AutoTok", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.project_root = root
    app.state.settings = cfg
    app.state.repo = repo
    app.state.jobs = JobRunner(repo, cfg)
    app.state.csrf_token = security.new_token()
    engines.apply_saved_model(app)
    if startup_scan:
        engines.start_background_scan(app)  # 每次啟動先掃描一次，確認模型沒有失效

    app.add_middleware(security.LocalOnlyMiddleware, token=app.state.csrf_token, allowed_hosts=allowed_hosts)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    for module in (today, research, studio, library, analytics, settings_page, notion, tiktok, engines, notifications, schedule):
        app.include_router(module.router)

    @app.post("/accounts/switch")
    def switch_account(request: Request, account_id: str = Form(...)) -> Response:
        if repo.get_account(account_id) is None:
            raise HTTPException(404, "找不到這個帳號")
        repo.set_active_account(account_id)
        resp = Response(status_code=204)
        resp.headers["HX-Refresh"] = "true"
        return resp

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"app": "autotok"}

    @app.get("/activity", response_class=HTMLResponse)
    def activity(request: Request, seen: int = 0) -> Response:
        job = app.state.jobs.current()
        finished_id = app.state.jobs.last_finished_id
        resp = templates.TemplateResponse(request, "_activity.html", {"request": request, "job": job})
        if finished_id and finished_id != seen and job is None:
            row = next((j for j in repo.list_jobs(limit=5) if j["id"] == finished_id), None)
            if row:
                ok = row["status"] == "ok"
                msg = f"{row['label']}：{row['summary'] or '完成'}" if ok else f"{row['label']} 失敗：{row['error']}"
                resp.headers["HX-Trigger"] = json.dumps({"jobFinished": {"kind": "ok" if ok else "err", "msg": msg}})
        return resp

    @app.get("/media/{rel_path:path}")
    def media(rel_path: str) -> Response:
        base = (root / "artifacts").resolve()
        try:
            target = (base / rel_path).resolve()
        except (ValueError, OSError):
            raise HTTPException(404) from None
        if not target.is_relative_to(base) or target.suffix.lower() not in ALLOWED_MEDIA_SUFFIXES:
            raise HTTPException(404)
        if not target.is_file():
            raise HTTPException(404)
        return FileResponse(target, media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")

    app.state.templates = templates
    app.state.local_dt = local_dt
    return app


def run_server(
    project_root: Path | None = None,
    settings: AppSettings | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if host not in security.LOCAL_HOSTS:
        raise SystemExit(f"操作台只能綁定本機位址（127.0.0.1 或 localhost），不接受 {host}。")
    app = create_app(project_root, settings, startup_scan=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
