"""成效：只顯示真實觀察資料；沒有資料就明講，不用模擬數字填空。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.analytics import AnalyticsAgent, compute_post_observation_metrics
from app.web.ui import render

router = APIRouter()


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


@router.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request) -> HTMLResponse:
    repo = request.app.state.repo
    active = repo.get_active_account()
    market = repo.get_market_profile(active.account_id)
    post_ids = {p.post_id for p in repo.list_posts(account_id=active.account_id)}
    all_obs = repo.list_post_observations(include_mock=True)
    real = [o for o in repo.list_post_observations(include_mock=False) if o.post_id in post_ids]
    hidden_mock = sum(1 for o in all_obs if o.is_mock)

    summaries = []
    rows = []
    if real:
        agent = AnalyticsAgent()
        for s in agent.summarize_by_slot(real):
            summaries.append(
                {
                    "slot": s.slot,
                    "track": market.track_name(s.slot),
                    "time": market.time_for(s.slot),
                    "posts": s.post_count,
                    "full_rate": _pct(s.avg_full_engagement_rate),
                    "save_share": _pct(s.avg_save_share_ratio),
                    "verified": s.verified_new_followers_total,
                    "correlated": s.correlated_new_followers_total,
                    "partial": s.partial_rate_post_count,
                }
            )
        for o in real:
            m = compute_post_observation_metrics(o)
            rows.append({"o": o, "rate": _pct(m.rate_value), "rate_kind": m.rate_type.value})
    last_observed = max((o.observed_at for o in real), default=None)
    return render(request, "analytics.html", "analytics", "成效", summaries=summaries, rows=rows, hidden_mock=hidden_mock, last_observed=last_observed)
