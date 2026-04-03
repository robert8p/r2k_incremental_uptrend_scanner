"""
Standalone research worker process.

Spawned as a subprocess by the web process when a research run is queued.
Reads settings from a JSON file, connects to the DB directly, and runs
the full research pipeline. All progress is written to the DB.

Usage:
    python -m app.services.research_worker \
        --run-id 42 --db-path ./data/scanner.db \
        --settings-path ./data/research_staging/research_settings_42.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.getcwd())

from app.config import Settings
from app.db import Database
from app.logging_config import setup_logging
from app.services.job_governance import complete_research_job, fail_research_job, update_research_progress
from app.services.research import (
    _offset_result_row,
    _parse_offset_ladder,
    _recommend_live_schedule,
    _weights_to_override_payload,
    _schedule_to_override_payload,
    LOCK_DIR,
)
from app.services.gate_audit import build_gate_audit_row, recommend_gate_audit_scenario
from app.services.goal_seek import run_goal_seek_optimization

logger = logging.getLogger(__name__)


def _acquire_lock(run_id: int) -> Path:
    lock_dir = Path(LOCK_DIR)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f'research_{run_id}.lock'
    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text().strip())
            os.kill(old_pid, 0)
            raise RuntimeError(
                f'Research run {run_id} is already running (PID {old_pid}). '
                f'Remove {lock_file} if the process is stale.'
            )
        except (ProcessLookupError, ValueError):
            logger.warning('Removing stale lock for run %d', run_id)
    lock_file.write_text(str(os.getpid()))
    return lock_file


def _release_lock(lock_file: Path) -> None:
    try:
        lock_file.unlink(missing_ok=True)
    except Exception:
        pass


def _run_single_validation_research(run_id, settings, db, alpaca, params):
    from app.config import save_settings_override
    from app.services.validation_engine import ValidationRunRequest, execute_validation_and_calibration

    def progress_callback(progress, message):
        update_research_progress(db, run_id, progress=progress, message=message)

    result = execute_validation_and_calibration(
        settings,
        db,
        alpaca,
        ValidationRunRequest(
            start_date=params['start_date'],
            end_date=params['end_date'],
            scan_offset_minutes=int(params['scan_offset_minutes']),
            cache_history=True,
        ),
        progress_callback=progress_callback,
    )
    payload = result['validation']
    calibration = result['calibration']
    auto_applied = False
    if bool(params.get('apply_recommended_weights')) and calibration.get('eligible') and calibration.get('should_apply') and calibration.get('recommended'):
        save_settings_override(settings, _weights_to_override_payload(calibration['recommended']['weights']))
        auto_applied = True
    return {'validation_id': payload['id'], 'best_validation_id': payload['id'], 'summary': payload['summary'],
            'calibration': calibration, 'auto_applied': auto_applied, 'auto_applied_schedule': False}


def _run_offset_ladder_research(run_id, settings, db, alpaca, params):
    from app.config import save_settings_override
    from app.services.validation_engine import ValidationRunRequest, execute_validation_and_calibration

    offsets = _parse_offset_ladder(','.join(str(v) for v in (params.get('offset_ladder') or [])), settings.research_offset_values)
    payloads_by_offset = {}
    offset_rows = []
    total = max(len(offsets), 1)
    for index, offset in enumerate(offsets):
        start_progress = 0.02 + (index / total) * 0.80
        end_progress = 0.02 + ((index + 1) / total) * 0.80
        def progress_callback(progress, message, *, _start=start_progress, _end=end_progress, _offset=offset):
            scaled = _start + (max(0.0, min(float(progress), 1.0)) * (_end - _start))
            db.update_research_run(run_id, status='running', progress=round(float(scaled), 4), message=f'Offset {_offset}: {message}')
        cycle = execute_validation_and_calibration(
            settings,
            db,
            alpaca,
            ValidationRunRequest(
                start_date=params['start_date'],
                end_date=params['end_date'],
                scan_offset_minutes=int(offset),
                cache_history=True,
            ),
            progress_callback=progress_callback,
        )
        payload = cycle['validation']
        payloads_by_offset[int(offset)] = payload
        offset_rows.append(_offset_result_row(payload, settings))

    update_research_progress(db, run_id, progress=0.90, message='Selecting recommended live schedule.')
    plan = _recommend_live_schedule(offset_rows, settings)
    best_validation_id = int(plan['primary_validation_id'])
    best_payload = next(p for p in payloads_by_offset.values() if int(p['id']) == best_validation_id)
    update_research_progress(db, run_id, progress=0.94, message='Using calibration attached to recommended offset.')
    calibration = dict((best_payload.get('summary') or {}).get('calibration') or {})
    auto_applied_weights = False
    if bool(params.get('apply_recommended_weights')) and calibration.get('eligible') and calibration.get('should_apply') and calibration.get('recommended'):
        save_settings_override(settings, _weights_to_override_payload(calibration['recommended']['weights']))
        auto_applied_weights = True
    auto_applied_schedule = False
    schedule_application_blocked_reason = None
    if bool(params.get('auto_apply_recommended_schedule')):
        if _schedule_is_actionable(plan):
            save_settings_override(settings, _schedule_to_override_payload(plan))
            auto_applied_schedule = True
        else:
            schedule_application_blocked_reason = str(plan.get('schedule_application_reason') or 'Recommended schedule remained advisory only.')
    return {'mode': 'offset_ladder_research', 'validation_id': best_validation_id, 'best_validation_id': best_validation_id,
            'validation_ids_by_offset': {str(o): int(p['id']) for o, p in payloads_by_offset.items()},
            'offset_ladder_summary': offset_rows, 'recommended_live_schedule': plan, 'summary': best_payload['summary'],
            'calibration': calibration, 'auto_applied': auto_applied_weights, 'auto_applied_schedule': auto_applied_schedule,
            'schedule_application_blocked_reason': schedule_application_blocked_reason}


def _settings_with_overrides(settings, overrides):
    payload = settings.model_dump(mode='python')
    payload.update({k: v for k, v in (overrides or {}).items()})
    return Settings(**payload)


def _run_gate_audit_research(run_id, settings, db, alpaca, params):
    from app.services.backtest import run_validation

    scenarios = list(params.get('scenarios') or [])
    if not scenarios:
        raise ValueError('Gate audit requires at least one scenario.')
    payloads = {}
    total = max(len(scenarios), 1)
    for index, scenario in enumerate(scenarios):
        scenario_name = str(scenario.get('name') or f'scenario_{index+1}')
        overrides = dict(scenario.get('overrides') or {})
        scenario_settings = _settings_with_overrides(settings, overrides)
        start_progress = 0.02 + (index / total) * 0.84
        end_progress = 0.02 + ((index + 1) / total) * 0.84
        def progress_callback(progress, message, *, _start=start_progress, _end=end_progress, _scenario_name=scenario_name):
            scaled = _start + (max(0.0, min(float(progress), 1.0)) * (_end - _start))
            db.update_research_run(run_id, status='running', progress=round(float(scaled), 4), message=f'Gate audit [{_scenario_name}]: {message}')
        payload = run_validation(
            scenario_settings,
            db,
            alpaca,
            start_date=params['start_date'],
            end_date=params['end_date'],
            scan_offset_minutes=int(params['scan_offset_minutes']),
            progress_callback=progress_callback,
            cache_history=True,
        )
        payloads[scenario_name] = {
            'payload': payload,
            'overrides': overrides,
        }

    update_research_progress(db, run_id, progress=0.92, message='Comparing baseline and relaxed scenarios.')
    baseline = payloads.get('baseline')
    if baseline is None:
        raise ValueError('Gate audit baseline scenario did not run.')
    audit_rows = [
        build_gate_audit_row(name, item['payload'], baseline_payload=baseline['payload'], overrides=item['overrides'])
        for name, item in payloads.items()
    ]
    recommendation = recommend_gate_audit_scenario(audit_rows)
    recommended_validation_id = int(recommendation.get('recommended_validation_id') or baseline['payload']['id'])
    selected_payload = next(
        item['payload'] for name, item in payloads.items()
        if int(item['payload']['id']) == recommended_validation_id
    )
    return {
        'mode': 'historical_counterfactual_gate_audit',
        'validation_id': recommended_validation_id,
        'best_validation_id': recommended_validation_id,
        'summary': selected_payload['summary'],
        'gate_audit_rows': audit_rows,
        'validation_ids_by_scenario': {name: int(item['payload']['id']) for name, item in payloads.items()},
        'recommended_gate_scenario': recommendation,
        'auto_applied': False,
        'auto_applied_schedule': False,
    }


def main():
    parser = argparse.ArgumentParser(description='R2K Research Worker')
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--db-path', type=str, required=True)
    parser.add_argument('--settings-path', type=str, required=True)
    args = parser.parse_args()

    settings_path = Path(args.settings_path)
    if not settings_path.exists():
        print(f'Settings file not found: {settings_path}', file=sys.stderr)
        sys.exit(1)
    settings = Settings(**json.loads(settings_path.read_text(encoding='utf-8')))
    setup_logging(settings, enable_stream=False)
    from app.services.alpaca_client import AlpacaClient
    db = Database(args.db_path)
    alpaca = AlpacaClient(settings)
    run = db.get_research_run(args.run_id)
    if not run:
        logger.error('Research run %d not found.', args.run_id)
        sys.exit(1)
    if run['status'] not in ('queued', 'running'):
        logger.warning('Run %d has status %s, skipping.', args.run_id, run['status'])
        sys.exit(0)

    lock_file = None
    try:
        lock_file = _acquire_lock(args.run_id)
    except RuntimeError as exc:
        logger.error(str(exc))
        fail_research_job(db, args.run_id, message=f'Lock failed: {exc}')
        sys.exit(1)

    params = run['params']
    update_research_progress(db, args.run_id, status='running', started_at=datetime.now(timezone.utc).isoformat(), progress=0.01, message='Research worker started (subprocess).')
    try:
        if str(params.get('mode') or '') == 'historical_counterfactual_gate_audit':
            result = _run_gate_audit_research(args.run_id, settings, db, alpaca, params)
            msg = 'Historical counterfactual gate audit completed.'
        elif str(params.get('mode') or '') == 'offline_goal_seek_optimization':
            def progress_callback(progress, message):
                update_research_progress(db, args.run_id, progress=round(float(progress), 4), message=message)
            result = run_goal_seek_optimization(args.run_id, settings, db, alpaca, params, progress_callback)
            msg = 'Offline goal-seek optimization completed.'
        elif bool(params.get('run_offset_ladder')):
            result = _run_offset_ladder_research(args.run_id, settings, db, alpaca, params)
            msg = 'Offset ladder research completed.'
        else:
            result = _run_single_validation_research(args.run_id, settings, db, alpaca, params)
            msg = 'Historical research run completed.'
        complete_research_job(db, args.run_id, result=result, message=msg)
        logger.info('Research run %d completed.', args.run_id)
    except Exception as exc:
        logger.exception('Research run %d failed: %s', args.run_id, exc)
        fail_research_job(db, args.run_id, message=f'{type(exc).__name__}: {exc}', result={'error': str(exc), 'error_type': type(exc).__name__, 'traceback_tail': traceback.format_exc().splitlines()[-8:]})
        sys.exit(1)
    finally:
        if lock_file:
            _release_lock(lock_file)
        try:
            settings_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == '__main__':
    main()
