from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.view_models import build_candidate_list, build_research_view, build_scan_view


def register_api_routes(
    app: FastAPI,
    *,
    runtime,
    version: str,
) -> None:
    @app.get('/healthz')
    def healthz() -> dict:
        settings = runtime.settings
        latest_scan = build_scan_view(runtime.repositories.scan.get_latest(), alpaca_data_feed=settings.alpaca_data_feed)
        from app.services.diagnostics import build_contract_health
        from app.services.live_trust import build_live_trust_snapshot
        from app.services.universe import load_universe
        universe = load_universe(settings, runtime.db, force_refresh=False)
        contract_health = build_contract_health(runtime.db)
        live_trust = build_live_trust_snapshot(settings, runtime.db)
        return {
            'ok': bool(contract_health.get('ok', True)),
            'app': settings.app_name,
            'version': version,
            'time_utc': datetime.now(timezone.utc).isoformat() + 'Z',
            'trading_mode': settings.trading_mode,
            'alpaca_credentials_present': runtime.alpaca.has_credentials(),
            'latest_scan_id': latest_scan['id'] if latest_scan else None,
            'universe_count': universe['status'].tradable_count,
            'contracts_ok': bool(contract_health.get('ok', True)),
            'contract_errors': contract_health.get('errors', []),
            'scheduler_status': runtime.scheduler_status,
            'live_trust_schedule_alignment_ok': live_trust.get('schedule_alignment_ok'),
            'live_trust_latest_research_run_id': live_trust.get('latest_research_run_id'),
        }

    @app.get('/status')
    def status() -> dict:
        from app.services.diagnostics import build_diagnostics_snapshot
        return build_diagnostics_snapshot(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            scheduler_status=runtime.scheduler_status,
        )

    @app.get('/api/settings/policies')
    def api_settings_policies() -> JSONResponse:
        return JSONResponse({
            'policy_groups': runtime.settings.policy_snapshot(),
            'policy_summary': runtime.settings.policy_summary(),
        })

    @app.get('/api/jobs')
    def api_jobs() -> JSONResponse:
        from app.services.job_governance import build_job_status_snapshot
        return JSONResponse(build_job_status_snapshot(runtime.repositories))

    @app.get('/api/latest-scan')
    def api_latest_scan() -> JSONResponse:
        settings = runtime.settings
        latest_scan = build_scan_view(runtime.repositories.scan.get_latest(), alpaca_data_feed=settings.alpaca_data_feed)
        if not latest_scan:
            return JSONResponse({'scan': None, 'candidates': []})
        return JSONResponse({'scan': latest_scan, 'candidates': build_candidate_list(runtime.repositories.scan.get_candidates(latest_scan['id']))})

    @app.get('/api/research/{run_id}')
    def api_research_run(run_id: int) -> JSONResponse:
        research = build_research_view(runtime.repositories.research.get(run_id))
        if not research:
            raise HTTPException(status_code=404, detail='Research run not found.')
        return JSONResponse(research)

    @app.get('/api/live-trust')
    def api_live_trust() -> JSONResponse:
        from app.services.live_trust import build_live_trust_snapshot
        return JSONResponse(build_live_trust_snapshot(runtime.settings, runtime.db))
