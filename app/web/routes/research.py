"""研究：蒐集爆款案例、依帳號市場計算適配度、把案例送去製作。"""

from __future__ import annotations

import csv
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app.fit import CaseFit, score_case
from app.models import ContentSlot, EvidenceLevel, ResearchCase, SourceType
from app.research_providers import ImportProvider, ImportTooLargeError, build_providers
from app.research_service import format_rate, refit_account, run_research, topic_from_case
from app.web.jobs import JobBusyError
from app.web.ui import render, toast

router = APIRouter()

SOURCE_ORDER = ("web", "import", "thirdparty", "mock")
RUN_LIMIT = {"web": 30, "thirdparty": 30, "mock": 35}
EVIDENCE_FILTERS = {"real": "真實來源", "all": "全部", "public": "公開頁面", "import": "手動匯入", "ad": "廣告樣本", "mock": "模擬"}
FIT_THRESHOLDS = (0, 40, 60, 80)


def evidence_view(case: ResearchCase) -> tuple[str, str, str]:
    """(篩選鍵, 標籤, chip 樣式)"""
    if case.is_mock or case.evidence_level == EvidenceLevel.MOCK_FIXTURE:
        return "mock", "模擬", "mock"
    if case.source_type == SourceType.AD:
        return "ad", "廣告樣本", "warn"
    if case.evidence_level == EvidenceLevel.MANUAL_IMPORT:
        return "import", "手動匯入", ""
    if case.evidence_level in (EvidenceLevel.VERIFIED_PUBLIC, EvidenceLevel.PARTIAL_PUBLIC):
        return "public", "公開頁面", ""
    return "public", "未驗證", ""


def _toast(msg: str, kind: str = "ok", **extra: object) -> Response:
    return toast(Response(status_code=200), msg, kind, **extra)


def _fit_from_row(case: ResearchCase, row: dict[str, object] | None, market) -> CaseFit:  # noqa: ANN001
    if row is None:
        return score_case(case, market)
    parts = score_case(case, market).parts  # 分項分數是決定性的，直接重算即可
    return CaseFit(
        case_id=case.case_id,
        score=float(row["score"]),  # type: ignore[arg-type]
        reason=str(row["reason"]),
        flag=str(row["flag"]),
        recommended_slot=ContentSlot(str(row["recommended_slot"])) if row["recommended_slot"] else ContentSlot.NOON_1130,
        parts=parts,
    )


@router.get("/research", response_class=HTMLResponse)
def research_view(
    request: Request, source: str = "", fit: int = 0, ev: str = "real", refit: int = 0
) -> HTMLResponse:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    providers = build_providers(cfg, market)
    provider_info = []
    for key in SOURCE_ORDER:
        p = providers[key]
        a = p.availability() if key != "import" else None
        provider_info.append(
            {
                "key": key,
                "label": p.label,
                "ok": True if a is None else a.ok,
                "reason": "" if a is None else a.reason,
                "verified": p.verified,
                "note": p.note,
            }
        )
    usable = [i["key"] for i in provider_info if i["ok"] and i["key"] in ("web", "thirdparty")]
    selected = source if source in providers else (usable[0] if usable else "import")

    cases = repo.list_research_cases(days=90)
    stored = repo.list_case_fits(active.account_id)
    if cases and (refit or len(stored) != len(cases)):
        refit_account(repo, active.account_id, market)
        stored = repo.list_case_fits(active.account_id)

    min_fit = fit if fit in FIT_THRESHOLDS else 0
    ev = ev if ev in EVIDENCE_FILTERS else "real"
    rows = []
    in_scope = 0
    for c in cases:
        f = _fit_from_row(c, stored.get(c.case_id), market)
        ev_key, ev_label, ev_kind = evidence_view(c)
        if ev == "real" and ev_key == "mock":
            continue
        if ev not in ("real", "all") and ev_key != ev:
            continue
        in_scope += 1
        if f.score < min_fit:
            continue
        rate, rate_note = format_rate(c)
        host = urlparse(c.source_url).netloc.replace("www.", "")
        rows.append(
            {
                "c": c,
                "fit": f,
                "ev_label": ev_label,
                "ev_kind": ev_kind,
                "rate": rate,
                "rate_note": rate_note or ("缺：觀看" if c.views is None else ""),
                "host": host,
                "title": c.title or c.title_zh or host,
                "slot_name": market.track_name(f.recommended_slot),
                "slot_time": market.time_for(f.recommended_slot),
            }
        )
    rows.sort(key=lambda r: r["fit"].score, reverse=True)

    last = repo.last_job(account_id=active.account_id, job="research", only_ok=True)
    last_any = repo.last_job(account_id=active.account_id, job="research")
    return render(
        request,
        "research.html",
        "research",
        "研究",
        rows=rows,
        total_cases=in_scope,
        any_cases=bool(cases),
        providers=provider_info,
        selected=selected,
        last=last,
        last_failed=last_any if last_any and last_any["status"] == "error" else None,
        fit_min=min_fit,
        fit_options=FIT_THRESHOLDS,
        ev=ev,
        ev_options=EVIDENCE_FILTERS,
        keywords=market.seed_keywords,
        brief=market.content_brief,
    )


@router.post("/research/run")
def research_run(request: Request, source: str = Form("web"), brief: str = Form("")) -> Response:
    repo = request.app.state.repo
    cfg = request.app.state.settings
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    brief = brief.strip()[:2000]
    if brief and brief != market.content_brief:
        market = market.model_copy(update={"content_brief": brief})  # 只用於這一次研究，不改動帳號設定
    providers = build_providers(cfg, market)
    provider = providers.get(source)
    if provider is None or source == "import":
        return _toast("這個來源要用「匯入」面板貼上資料", "err")
    avail = provider.availability()
    if not avail.ok:
        return _toast(avail.reason, "err")

    def job(handle) -> str:  # noqa: ANN001
        summary = run_research(
            repo,
            account_id=active.account_id,
            market=market,
            provider=provider,
            max_cases=RUN_LIMIT.get(source, 30),
            progress=handle.progress,
        )
        if market.content_brief:
            summary += f"（訴求：{market.content_brief[:30]}{'…' if len(market.content_brief) > 30 else ''}）"
        return summary

    try:
        request.app.state.jobs.submit(account_id=active.account_id, job="research", label=f"爆款研究（{provider.label}）", fn=job)
    except JobBusyError as exc:
        return _toast(str(exc), "err")
    return _toast(f"已開始研究（{provider.label}）")


@router.post("/research/brief/save")
def research_brief_save(request: Request, brief: str = Form("")) -> Response:
    """把研究頁的訴求存成這個帳號的預設內容方向（市場設定）。"""
    repo = request.app.state.repo
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    market.content_brief = brief.strip()[:2000]
    repo.save_market_profile(active.account_id, market)
    return _toast("已存成這個帳號的預設內容方向")


MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@router.post("/research/import")
def research_import(request: Request, text: str = Form(""), file: UploadFile | None = None) -> Response:
    repo = request.app.state.repo
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    body = text
    if file is not None and file.filename:
        raw = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(raw) > MAX_UPLOAD_BYTES:
            return _toast("檔案超過 2 MB，請拆成較小的檔案", "err")
        body = raw.decode("utf-8-sig", errors="replace")
    if len(body.encode("utf-8")) > MAX_UPLOAD_BYTES:
        return _toast("貼上的內容超過 2 MB，請拆成較小的部分", "err")
    if not body.strip():
        return _toast("請貼上網址、CSV 或 JSON，或選擇檔案", "err")
    provider = ImportProvider(market, body)
    try:
        cases = provider.collect_cases()
    except ImportTooLargeError as exc:
        return _toast(str(exc), "err")
    except (ValueError, csv.Error, ArithmeticError):
        return _toast("內容不是有效的 JSON 或 CSV", "err")
    if not cases:
        return _toast("沒有找到任何有效的 http(s) 網址，沒有寫入資料", "err")

    def job(handle) -> str:  # noqa: ANN001
        for c in cases:
            repo.upsert_research_case(c)
        refit_account(repo, active.account_id, market)
        missing = sum(1 for c in cases if c.views is None)
        return f"手動匯入：{len(cases)} 筆，{missing} 筆沒有觀看數"

    try:
        request.app.state.jobs.submit(account_id=active.account_id, job="research", label="爆款研究（手動匯入）", fn=job)
    except JobBusyError as exc:
        return _toast(str(exc), "err")
    return _toast(f"已開始匯入 {len(cases)} 筆，完成後頁面會自動更新")


@router.post("/research/refit")
def research_refit(request: Request) -> Response:
    repo = request.app.state.repo
    active = repo.get_active_account()
    fits = refit_account(repo, active.account_id, repo.get_market_profile(active.account_id))
    return _toast(f"已依目前市場設定重新計算 {len(fits)} 筆", refresh=True)


@router.post("/research/send")
def research_send(request: Request, case_id: str = Form(...)) -> Response:
    repo = request.app.state.repo
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    case = next((c for c in repo.list_research_cases(days=90) if c.case_id == case_id), None)
    if case is None:
        return _toast("找不到這個案例", "err")
    stored = repo.list_case_fits(active.account_id).get(case_id)
    fit = _fit_from_row(case, stored, market)
    topic, reason = topic_from_case(repo, account_id=active.account_id, market=market, case=case, fit=fit)
    if topic is None:
        return _toast(reason, "err")
    resp = Response(status_code=200)
    resp.headers["HX-Redirect"] = f"/studio?topic={topic.topic_id}"
    return resp
