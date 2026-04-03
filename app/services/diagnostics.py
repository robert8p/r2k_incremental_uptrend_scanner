from __future__ import annotations

from typing import Dict, List, Optional

from app.contracts import (
    normalize_candidate_payload,
    normalize_research_result,
    normalize_scan_summary,
    normalize_validation_summary,
)

from app.config import Settings
from app.db import Database
from app.services.alpaca_client import AlpacaClient
from app.services.live_trust import build_live_trust_snapshot
from app.services.universe import load_universe


def build_diagnostics_snapshot(
    settings: Settings,
    db: Database,
    alpaca: AlpacaClient,
    *,
    scheduler_status: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    universe = load_universe(settings, db, force_refresh=False)
    latest_scan = db.get_latest_scan()
    data_ping = alpaca.ping_data_api()
    account_status = None
    if settings.trading_mode in {'paper', 'live'} and alpaca.has_credentials():
        try:
            account_status = alpaca.get_account(settings.trading_mode)
        except Exception as exc:
            account_status = {'error': str(exc)}

    from app.services.market_time import latest_or_previous_trading_day

    from app.services.decision_bundle import get_or_build_decision_state

    return {
        'app_env': settings.app_env,
        'trading_mode': settings.trading_mode,
        'enable_live_trading': settings.enable_live_trading,
        'latest_trading_day': latest_or_previous_trading_day(),
        'data_api': data_ping,
        'account_status': account_status,
        'latest_scan': latest_scan,
        'universe_status': universe['status'].__dict__,
        'config': settings.public_snapshot(),
        'contract_health': build_contract_health(db),
        'scheduler_status': scheduler_status or {'leader': False, 'scheduler_running': False, 'mode': 'unknown'},
        'live_trust': build_live_trust_snapshot(settings, db),
        'decision_state': get_or_build_decision_state(settings, db, alpaca),
    }


def read_recent_logs(settings: Settings, max_lines: int = 200) -> List[str]:
    from pathlib import Path

    log_path = Path(settings.data_dir) / 'logs' / 'app.log'
    if not log_path.exists():
        return ['No log file yet.']
    lines = log_path.read_text(encoding='utf-8').splitlines()
    return lines[-max_lines:]


def build_contract_health(db: Database) -> Dict[str, object]:
    report: Dict[str, object] = {
        'ok': True,
        'latest_scan_ok': True,
        'latest_validation_ok': True,
        'latest_research_ok': True,
        'candidate_contract_errors': 0,
        'errors': [],
    }

    try:
        latest_scan = db.get_latest_scan()
    except Exception as exc:
        report['ok'] = False
        report['latest_scan_ok'] = False
        report['errors'].append(f'latest_scan_load: {type(exc).__name__}: {exc}')
        latest_scan = None
    if latest_scan:
        try:
            normalize_scan_summary(latest_scan.get('summary'))
            try:
                candidates = db.get_scan_candidates(int(latest_scan['id']))
            except Exception as exc:
                report['ok'] = False
                report['latest_scan_ok'] = False
                report['errors'].append(f'latest_scan_candidates: {type(exc).__name__}: {exc}')
                candidates = []
            for candidate in candidates:
                try:
                    normalize_candidate_payload(candidate)
                except Exception as exc:
                    report['ok'] = False
                    report['latest_scan_ok'] = False
                    report['candidate_contract_errors'] += 1
                    report['errors'].append(f"scan_candidate[{candidate.get('symbol', '?')}]: {type(exc).__name__}: {exc}")
        except Exception as exc:
            report['ok'] = False
            report['latest_scan_ok'] = False
            report['errors'].append(f'latest_scan: {type(exc).__name__}: {exc}')

    try:
        validations = db.list_validation_runs(limit=1)
    except Exception as exc:
        report['ok'] = False
        report['latest_validation_ok'] = False
        report['errors'].append(f'latest_validation_load: {type(exc).__name__}: {exc}')
        validations = []
    if validations:
        try:
            normalize_validation_summary((validations[0] or {}).get('summary'))
        except Exception as exc:
            report['ok'] = False
            report['latest_validation_ok'] = False
            report['errors'].append(f'latest_validation: {type(exc).__name__}: {exc}')

    try:
        research_runs = db.list_research_runs(limit=1)
    except Exception as exc:
        report['ok'] = False
        report['latest_research_ok'] = False
        report['errors'].append(f'latest_research_load: {type(exc).__name__}: {exc}')
        research_runs = []
    if research_runs and research_runs[0].get('result') is not None:
        try:
            normalize_research_result((research_runs[0] or {}).get('result'))
        except Exception as exc:
            report['ok'] = False
            report['latest_research_ok'] = False
            report['errors'].append(f'latest_research: {type(exc).__name__}: {exc}')

    return report
