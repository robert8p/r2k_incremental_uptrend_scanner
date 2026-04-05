from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import plotly.graph_objects as go
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ModuleNotFoundError:  # pragma: no cover - lightweight test/review environments
    class BackgroundScheduler:  # type: ignore[override]
        def __init__(self, *args, **kwargs):
            self.running = False

        def add_job(self, *args, **kwargs):
            return None

        def start(self):
            self.running = True

        def shutdown(self, wait: bool = False):
            self.running = False

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import TokenAuthMiddleware
from app.logging_config import setup_logging
from app.routes.admin_routes import register_admin_routes
from app.routes.api_routes import register_api_routes
from app.routes.scan_routes import register_scan_routes
from app.routes.validation_routes import register_validation_routes
from app.runtime import AppRuntime
from app.services.scheduler_guard import SchedulerLeaderGuard
from app.version import VERSION

logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent


def create_app(runtime: Optional[AppRuntime] = None) -> FastAPI:
    runtime = runtime or AppRuntime()
    setup_logging(runtime.settings)

    scheduler = BackgroundScheduler(timezone='America/New_York')
    lock_path = Path(runtime.settings.data_dir) / 'locks' / 'scheduler.lock'
    scheduler_guard = SchedulerLeaderGuard(str(lock_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.services.research import recover_stale_research_runs

        runtime.refresh()
        logger.info('Starting %s v%s', runtime.settings.app_name, VERSION)
        recovered = recover_stale_research_runs(runtime.db)
        if recovered:
            logger.warning('Recovered %d stale research run(s) on startup.', recovered)
        scheduler_started = False
        if runtime.settings.enable_scheduler:
            is_leader = scheduler_guard.acquire()
            if is_leader and not scheduler.running:
                scheduler.add_job(scheduled_scan_tick, 'interval', minutes=2, id='scheduled_scan_tick', replace_existing=True)
                scheduler.start()
                scheduler_started = True
                logger.info('Background scheduler started as leader process.')
            elif not is_leader:
                logger.warning('Background scheduler suppressed in follower process.')
        else:
            logger.info('Background scheduler disabled by settings.')
        runtime.scheduler_status = scheduler_guard.status(scheduler_running=scheduler.running)
        try:
            from app.services.evidence_automation import refresh_evidence_smoke_validation_cache
            route_paths = {route.path for route in app.router.routes}
            refresh_evidence_smoke_validation_cache(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                route_paths=route_paths,
                scheduler_status=runtime.scheduler_status,
                reason='startup',
            )
        except Exception as exc:
            logger.exception('Evidence smoke validation failed on startup: %s', exc)
        try:
            yield
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            scheduler_guard.release()
            runtime.scheduler_status = scheduler_guard.status(scheduler_running=False)
            if scheduler_started:
                logger.info('Background scheduler stopped and leader lock released.')

    app = FastAPI(title=runtime.settings.app_name, version=VERSION, lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(TokenAuthMiddleware, auth_token=runtime.settings.auth_token)
    app.mount('/static', StaticFiles(directory=str(APP_DIR / 'static')), name='static')
    templates = Jinja2Templates(directory=str(APP_DIR / 'templates'))

    def safe_render(request: Request, template: str, context: Dict[str, Any]) -> HTMLResponse:
        token = request.query_params.get('token', '').strip()
        base_context = {
            'request': request,
            'app_name': runtime.settings.app_name,
            'version': VERSION,
            'settings_snapshot': runtime.settings.public_snapshot(),
            'token_suffix': f'?token={token}' if token else '',
        }
        base_context.update(context)
        return templates.TemplateResponse(template, base_context)

    def scheduled_scan_tick() -> None:
        current_settings = runtime.refresh()
        current_alpaca = runtime.alpaca
        runtime.scheduler_status = scheduler_guard.status(scheduler_running=scheduler.running)
        if not current_alpaca.has_credentials():
            return
        from app.services.decision_bundle import maybe_refresh_decision_bundle_after_close
        from app.services.evidence_automation import maybe_refresh_evidence_automation_after_close
        from app.services.live_trust import evaluate_pending_live_outcomes
        from app.services.market_time import get_session_for_day, latest_or_previous_trading_day
        from app.services.scanner import run_scan

        day = latest_or_previous_trading_day()
        try:
            evaluate_pending_live_outcomes(current_settings, runtime.db, current_alpaca)
            for offset in current_settings.scheduled_offsets:
                session = get_session_for_day(day, offset)
                now_et = session.now_et
                if abs((now_et - session.checkpoint).total_seconds()) <= 180:
                    existing = [scan for scan in runtime.db.list_scans(limit=20) if scan['trading_day'] == day and scan['scan_offset_minutes'] == offset]
                    if existing:
                        continue
                    logger.info('Scheduler triggering scan for %s at %s minutes.', day, offset)
                    run_scan(current_settings, runtime.db, current_alpaca, trading_day=day, offset_minutes=offset)
            maybe_refresh_decision_bundle_after_close(current_settings, runtime.db, current_alpaca, days=60, offsets=[120, 150])
            maybe_refresh_evidence_automation_after_close(
                current_settings,
                runtime.db,
                current_alpaca,
                days=60,
                offsets=[120, 150],
                review_days=10,
                lookback_days=90,
                route_paths={route.path for route in app.router.routes},
                scheduler_status=runtime.scheduler_status,
            )
        except Exception as exc:
            logger.exception('Scheduled scan tick failed: %s', exc)

    def build_candidate_chart(scan: Dict[str, Any], candidate: Dict[str, Any]) -> str:
        from app.services.market_time import get_session_for_day

        trading_day = scan['trading_day']
        session = get_session_for_day(trading_day, scan['scan_offset_minutes'])
        bars_map = runtime.alpaca.fetch_bars([candidate['symbol']], '1Min', session.market_open.isoformat(), session.market_close.isoformat())
        df = bars_map.get(candidate['symbol'])
        if df is None or df.empty:
            return ''
        fig = go.Figure(
            data=[
                go.Candlestick(
                    x=df['timestamp'],
                    open=df['open'],
                    high=df['high'],
                    low=df['low'],
                    close=df['close'],
                    name=candidate['symbol'],
                )
            ]
        )
        ctx = candidate.get('chart_context') or {}
        for name, value, dash in [
            ('Band low', ctx.get('band_low'), 'dot'),
            ('Band high', ctx.get('band_high'), 'dot'),
            ('Entry low', ctx.get('entry_low'), 'dash'),
            ('Entry high', ctx.get('entry_high'), 'dash'),
            ('Target', ctx.get('target_price'), 'solid'),
            ('Stop', ctx.get('stop_price'), 'solid'),
        ]:
            if value is None:
                continue
            fig.add_hline(y=value, line_dash=dash, annotation_text=name)
        fig.update_layout(height=520, margin=dict(l=20, r=20, t=40, b=20), xaxis_rangeslider_visible=False)
        return fig.to_html(full_html=False, include_plotlyjs='cdn')

    def validation_chart_html(summary: Dict[str, Any]) -> str:
        fig = go.Figure()
        score_rows = summary.get('score_bucket_summary', [])
        if score_rows:
            fig.add_bar(x=[row['bucket'] for row in score_rows], y=[row['hit_rate'] for row in score_rows], name='Hit rate by score bucket')
        mover_rows = summary.get('mover_bucket_summary', [])
        if mover_rows:
            fig.add_scatter(x=[row['bucket'] for row in mover_rows], y=[row['hit_rate'] for row in mover_rows], mode='lines+markers', name='Hit rate by mover rank bucket')
        baseline = (summary.get('baseline_comparison') or {}).get('mover_rank_only') or {}
        stage2 = {
            'precision_at_5': summary.get('precision_at_5'),
            'precision_at_10': summary.get('precision_at_10'),
            'precision_at_20': summary.get('precision_at_20'),
        }
        if baseline:
            fig.add_bar(
                x=['P@5', 'P@10', 'P@20'],
                y=[baseline.get('precision_at_5', 0.0), baseline.get('precision_at_10', 0.0), baseline.get('precision_at_20', 0.0)],
                name='Mover-rank-only baseline',
            )
            fig.add_bar(
                x=['P@5', 'P@10', 'P@20'],
                y=[stage2.get('precision_at_5', 0.0), stage2.get('precision_at_10', 0.0), stage2.get('precision_at_20', 0.0)],
                name='Stage-2 score',
            )
        fig.update_layout(height=480, margin=dict(l=20, r=20, t=40, b=20), yaxis_title='Hit rate / precision', barmode='group')
        return fig.to_html(full_html=False, include_plotlyjs='cdn')

    register_api_routes(app, runtime=runtime, version=VERSION)
    register_scan_routes(app, runtime=runtime, safe_render=safe_render, build_candidate_chart=build_candidate_chart)
    register_validation_routes(app, runtime=runtime, safe_render=safe_render, validation_chart_html=validation_chart_html)
    register_admin_routes(app, runtime=runtime, safe_render=safe_render)

    return app
