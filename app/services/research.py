"""
Research orchestration module (subprocess-based).

Queues research runs in the DB, spawns a subprocess worker, handles
lock-based concurrency control, and recovers stale runs on startup.

The actual work runs in research_worker.py as a detached subprocess.
This means research jobs survive web process restarts.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd

from app.config import Settings, save_settings_override
from app.db import Database
from app.services.job_governance import fail_research_job, interrupt_research_job, queue_research_job
from app.services.telemetry import emit_event

logger = logging.getLogger(__name__)

LOCK_DIR = os.environ.get('RESEARCH_LOCK_DIR', './data/locks')
SETTINGS_STAGING_DIR = os.environ.get('RESEARCH_SETTINGS_DIR', './data/research_staging')


def _worker_log_path(settings: Settings, run_id: int) -> Path:
    log_dir = Path(settings.data_dir) / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f'research_worker_{run_id}.log'


def _spawn_research_worker(*, settings: Settings, run_id: int, db_path: str, settings_file: Path, spawn_label: str) -> None:
    worker_cmd = [
        sys.executable,
        '-m',
        'app.services.research_worker',
        '--run-id',
        str(run_id),
        '--db-path',
        db_path,
        '--settings-path',
        str(settings_file),
    ]
    log_path = _worker_log_path(settings, run_id)
    env = os.environ.copy()
    env.setdefault('PYTHONUNBUFFERED', '1')
    log_handle = log_path.open('ab')
    try:
        process = subprocess.Popen(
            worker_cmd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    except Exception:
        log_handle.close()
        raise
    else:
        log_handle.close()
        logger.info('Spawned %s worker for run %d (PID %d).', spawn_label, run_id, process.pid)
        emit_event('research.spawned', run_id=int(run_id), pid=int(process.pid), settings_path=str(settings_file), worker_log_path=str(log_path), spawn_label=spawn_label)


def _weights_to_override_payload(weights: Dict[str, float]) -> Dict[str, float]:
    return {
        'weight_target_strength': float(weights['target_strength']),
        'weight_liquidity': float(weights['liquidity']),
        'weight_volatility': float(weights['volatility_capacity']),
        'weight_dynamic_range': float(weights['dynamic_range']),
        'weight_range_position': float(weights['range_position']),
        'weight_time_feasibility': float(weights['time_feasibility']),
        'weight_execution_quality': float(weights['execution_quality']),
    }


def _schedule_to_override_payload(plan: Dict[str, object]) -> Dict[str, object]:
    return {
        'default_scan_offset_minutes': int(plan['default_scan_offset_minutes']),
        'scheduled_scan_offsets': str(plan['suggested_scheduled_scan_offsets']),
    }


def _parse_offset_ladder(raw: str | None, fallback: List[int]) -> List[int]:
    values: List[int] = []
    for part in str(raw or '').split(','):
        token = part.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError:
            continue
        if value > 0:
            values.append(value)
    deduped = sorted(set(values))
    if deduped:
        return deduped
    return sorted(set(int(v) for v in fallback if int(v) > 0)) or [120]


def _offset_result_row(payload: Dict[str, object], settings: Settings) -> Dict[str, object]:
    summary = payload['summary']
    funnel = summary.get('stage_funnel_summary') or {}
    validation_verdict = (summary.get('validation_verdict') or {}).get('verdict') or 'UNKNOWN'
    advanced_rows = int(summary.get('advanced_stage2_total') or 0)
    actionable_lane = int(funnel.get('actionable_lane') or 0)
    precision_at_10 = float(summary.get('precision_at_10') or 0.0)
    conditional_precision_at_10 = float(summary.get('conditional_precision_at_10_entry_touched') or 0.0)
    overall_hit_rate = float(summary.get('overall_hit_rate') or 0.0)
    entry_touch_rate = float(summary.get('entry_touch_rate_stage2') or 0.0)
    stage2_from_scored = float((funnel.get('rates') or {}).get('advanced_from_scored') or 0.0)
    actionable_from_advanced = float((funnel.get('rates') or {}).get('actionable_lane_from_advanced') or 0.0)
    precision_score = min(max(precision_at_10, 0.0), 1.0)
    conditional_score = min(max(conditional_precision_at_10, 0.0), 1.0)
    sample_score = min(advanced_rows / max(float(settings.ladder_min_advanced_rows), 1.0), 1.5) / 1.5
    actionable_score = min(actionable_lane / 8.0, 1.0)
    entry_touch_score = min(max(entry_touch_rate, 0.0), 1.0)
    utility_score = round(precision_score * 0.42 + conditional_score * 0.16 + sample_score * 0.22 + actionable_score * 0.12 + entry_touch_score * 0.08, 4)
    clears_quality_gate = precision_at_10 >= float(settings.ladder_min_precision_at_10)
    clears_sample_gate = advanced_rows >= int(settings.ladder_min_advanced_rows)
    return {
        'scan_offset_minutes': int(payload['scan_offset_minutes']), 'validation_id': int(payload['id']),
        'days': int(summary.get('days') or 0), 'advanced_stage2_total': advanced_rows, 'actionable_lane': actionable_lane,
        'precision_at_5': round(float(summary.get('precision_at_5') or 0.0), 4), 'precision_at_10': round(precision_at_10, 4),
        'precision_at_20': round(float(summary.get('precision_at_20') or 0.0), 4),
        'conditional_precision_at_10_entry_touched': round(conditional_precision_at_10, 4),
        'overall_hit_rate': round(overall_hit_rate, 4), 'entry_touch_rate_stage2': round(entry_touch_rate, 4),
        'advanced_from_scored': round(stage2_from_scored, 4), 'actionable_lane_from_advanced': round(actionable_from_advanced, 4),
        'validation_verdict': validation_verdict, 'utility_score': utility_score,
        'clears_quality_gate': clears_quality_gate, 'clears_sample_gate': clears_sample_gate, 'recommended': False,
    }


def _recommend_live_schedule(offset_rows: List[Dict[str, object]], settings: Settings) -> Dict[str, object]:
    if not offset_rows:
        return {
            'default_scan_offset_minutes': int(settings.default_scan_offset_minutes),
            'secondary_scan_offset_minutes': None,
            'suggested_scheduled_scan_offsets': str(settings.scheduled_scan_offsets),
            'recommendation_basis': 'No offset results were available.',
            'all_offsets_failed_gates': True,
            'schedule_should_apply': False,
            'schedule_application_reason': 'No offset results were available, so the schedule remains advisory only.',
        }
    eligible = [r for r in offset_rows if bool(r.get('clears_quality_gate')) and bool(r.get('clears_sample_gate'))]
    ranked_pool = sorted(
        eligible or offset_rows,
        key=lambda r: (float(r.get('utility_score') or 0), float(r.get('precision_at_10') or 0), int(r.get('advanced_stage2_total') or 0)),
        reverse=True,
    )
    primary = ranked_pool[0]
    secondary = ranked_pool[1] if len(ranked_pool) > 1 else None
    recommended_offsets = sorted({int(primary['scan_offset_minutes'])} | ({int(secondary['scan_offset_minutes'])} if secondary else set()))
    primary['recommended'] = True
    if secondary is not None:
        secondary['recommended'] = True
    schedule_should_apply = bool(eligible)
    if eligible:
        basis = f"Selected offsets clearing quality gate (P@10 >= {settings.ladder_min_precision_at_10}) and sample gate (advanced >= {settings.ladder_min_advanced_rows}), ranked by utility."
        apply_reason = 'Primary recommendation cleared both ladder gates and is safe to apply as a live schedule candidate.'
    else:
        basis = 'No offset cleared both gates; fell back to strongest utility score.'
        apply_reason = 'No offset cleared both ladder gates, so the recommendation remains advisory only and should not be auto-applied.'
    return {
        'default_scan_offset_minutes': int(primary['scan_offset_minutes']),
        'secondary_scan_offset_minutes': int(secondary['scan_offset_minutes']) if secondary else None,
        'suggested_scheduled_scan_offsets': ','.join(str(v) for v in recommended_offsets),
        'recommendation_basis': basis,
        'all_offsets_failed_gates': not bool(eligible),
        'schedule_should_apply': schedule_should_apply,
        'schedule_application_reason': apply_reason,
        'primary_validation_id': int(primary['validation_id']),
        'secondary_validation_id': int(secondary['validation_id']) if secondary else None,
    }


def _schedule_is_actionable(plan: Dict[str, object]) -> bool:
    return bool(plan) and bool(plan.get('suggested_scheduled_scan_offsets')) and not bool(plan.get('all_offsets_failed_gates')) and bool(plan.get('schedule_should_apply', True))


# ---------------------------------------------------------------------------
# Subprocess launching
# ---------------------------------------------------------------------------

def _has_active_research_subprocess(run_id: int) -> bool:
    lock_file = Path(LOCK_DIR) / f'research_{run_id}.lock'
    if not lock_file.exists():
        return False
    try:
        pid = int(lock_file.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, OSError):
        return False


def _count_active_research_runs() -> int:
    lock_dir = Path(LOCK_DIR)
    if not lock_dir.exists():
        return 0
    count = 0
    for lock_file in lock_dir.glob('research_*.lock'):
        try:
            pid = int(lock_file.read_text().strip())
            os.kill(pid, 0)
            count += 1
        except (ProcessLookupError, ValueError, OSError):
            pass
    return count


def start_three_month_research_run(
    settings: Settings, db: Database, *,
    scan_offset_minutes: int, end_date: str | None = None,
    apply_recommended_weights: bool = False, run_offset_ladder: bool = False,
    offset_ladder: str | None = None, auto_apply_recommended_schedule: bool = False,
) -> int:
    if _count_active_research_runs() >= 1:
        raise RuntimeError('A research run is already in progress. Wait for it to complete.')

    if end_date:
        chosen_end = end_date
    else:
        from app.services.market_time import latest_or_previous_trading_day
        chosen_end = latest_or_previous_trading_day()
    start_date = (pd.Timestamp(chosen_end) - pd.DateOffset(months=settings.research_lookback_months)).strftime('%Y-%m-%d')
    ladder_values = _parse_offset_ladder(offset_ladder, settings.research_offset_values)
    params = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'three_month_offset_ladder_research' if run_offset_ladder else 'three_month_validation_and_calibration',
        'start_date': start_date, 'end_date': chosen_end, 'scan_offset_minutes': int(scan_offset_minutes),
        'lookback_months': int(settings.research_lookback_months),
        'apply_recommended_weights': bool(apply_recommended_weights), 'run_offset_ladder': bool(run_offset_ladder),
        'offset_ladder': ladder_values, 'offset_ladder_raw': offset_ladder or settings.research_offset_ladder,
        'auto_apply_recommended_schedule': bool(auto_apply_recommended_schedule),
    }
    run_id = queue_research_job(db, params, message='Queued historical research run.')
    emit_event('research.queued', run_id=int(run_id), mode=params['mode'], start_date=start_date, end_date=chosen_end, scan_offset_minutes=int(scan_offset_minutes), run_offset_ladder=bool(run_offset_ladder))

    staging_dir = Path(SETTINGS_STAGING_DIR)
    staging_dir.mkdir(parents=True, exist_ok=True)
    settings_file = staging_dir / f'research_settings_{run_id}.json'
    settings_file.write_text(json.dumps(settings.model_dump(), default=str), encoding='utf-8')

    try:
        _spawn_research_worker(settings=settings, run_id=run_id, db_path=db.path, settings_file=settings_file, spawn_label='research')
    except Exception as exc:
        logger.exception('Failed to spawn research worker for run %d: %s', run_id, exc)
        fail_research_job(db, run_id, message=f'Failed to spawn worker: {exc}')
        emit_event('research.spawn_failed', level='error', run_id=int(run_id), error_type=type(exc).__name__, error=str(exc))
        settings_file.unlink(missing_ok=True)
        raise
    return run_id


def start_gate_audit_run(
    settings: Settings,
    db: Database,
    *,
    start_date: str,
    end_date: str,
    scan_offset_minutes: int,
    scenario_a_name: str,
    scenario_a_min_avg_dollar_volume: float,
    scenario_a_min_price: float,
    scenario_a_low_price_hard_floor: float,
    scenario_b_name: str,
    scenario_b_min_avg_dollar_volume: float,
    scenario_b_min_price: float,
    scenario_b_low_price_hard_floor: float,
) -> int:
    if _count_active_research_runs() >= 1:
        raise RuntimeError('A research-like job is already in progress. Wait for it to complete.')

    params = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'historical_counterfactual_gate_audit',
        'start_date': start_date,
        'end_date': end_date,
        'scan_offset_minutes': int(scan_offset_minutes),
        'scenarios': [
            {'name': 'baseline', 'overrides': {}},
            {
                'name': str(scenario_a_name or 'liquidity_relaxed').strip() or 'liquidity_relaxed',
                'overrides': {
                    'min_avg_dollar_volume': float(scenario_a_min_avg_dollar_volume),
                    'min_price': float(scenario_a_min_price),
                    'low_price_hard_floor': float(scenario_a_low_price_hard_floor),
                },
            },
            {
                'name': str(scenario_b_name or 'liquidity_and_price_relaxed').strip() or 'liquidity_and_price_relaxed',
                'overrides': {
                    'min_avg_dollar_volume': float(scenario_b_min_avg_dollar_volume),
                    'min_price': float(scenario_b_min_price),
                    'low_price_hard_floor': float(scenario_b_low_price_hard_floor),
                },
            },
        ],
    }
    run_id = queue_research_job(db, params, message='Queued historical gate audit.')
    emit_event('research.queued', run_id=int(run_id), mode=params['mode'], start_date=start_date, end_date=end_date, scan_offset_minutes=int(scan_offset_minutes), run_offset_ladder=False)

    staging_dir = Path(SETTINGS_STAGING_DIR)
    staging_dir.mkdir(parents=True, exist_ok=True)
    settings_file = staging_dir / f'research_settings_{run_id}.json'
    settings_file.write_text(json.dumps(settings.model_dump(), default=str), encoding='utf-8')

    try:
        _spawn_research_worker(settings=settings, run_id=run_id, db_path=db.path, settings_file=settings_file, spawn_label='gate_audit')
    except Exception as exc:
        logger.exception('Failed to spawn gate audit worker for run %d: %s', run_id, exc)
        fail_research_job(db, run_id, message=f'Failed to spawn worker: {exc}')
        emit_event('research.spawn_failed', level='error', run_id=int(run_id), error_type=type(exc).__name__, error=str(exc))
        settings_file.unlink(missing_ok=True)
        raise
    return run_id


def start_goal_seek_run(
    settings: Settings,
    db: Database,
    *,
    start_date: str,
    end_date: str,
    train_days: int,
    test_days: int,
    step_days: int,
    embargo_days: int,
    offsets: str | None = None,
    config_scope: str = 'full',
) -> int:
    if _count_active_research_runs() >= 1:
        raise RuntimeError('A research-like job is already in progress. Wait for it to complete.')

    params = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'mode': 'offline_goal_seek_optimization',
        'start_date': start_date,
        'end_date': end_date,
        'train_days': int(train_days),
        'test_days': int(test_days),
        'step_days': int(step_days),
        'embargo_days': int(embargo_days),
        'offsets': _parse_offset_ladder(offsets, [120, 150]),
        'config_scope': str(config_scope or 'full').strip() or 'full',
    }
    run_id = queue_research_job(db, params, message='Queued offline goal-seek optimization.')
    emit_event('research.queued', run_id=int(run_id), mode=params['mode'], start_date=start_date, end_date=end_date, offsets=params['offsets'], config_scope=params['config_scope'])

    staging_dir = Path(SETTINGS_STAGING_DIR)
    staging_dir.mkdir(parents=True, exist_ok=True)
    settings_file = staging_dir / f'research_settings_{run_id}.json'
    settings_file.write_text(json.dumps(settings.model_dump(), default=str), encoding='utf-8')

    try:
        _spawn_research_worker(settings=settings, run_id=run_id, db_path=db.path, settings_file=settings_file, spawn_label='goal_seek')
    except Exception as exc:
        logger.exception('Failed to spawn goal-seek worker for run %d: %s', run_id, exc)
        fail_research_job(db, run_id, message=f'Failed to spawn worker: {exc}')
        emit_event('research.spawn_failed', level='error', run_id=int(run_id), error_type=type(exc).__name__, error=str(exc))
        settings_file.unlink(missing_ok=True)
        raise
    return run_id



# ---------------------------------------------------------------------------
# Stale-run recovery (call on startup)
# ---------------------------------------------------------------------------

def recover_stale_research_runs(db: Database) -> int:
    recovered = 0
    for run in db.list_research_runs(limit=50):
        if run['status'] in ('running', 'queued'):
            run_id = run['id']
            if _has_active_research_subprocess(run_id):
                logger.info('Research run %d still has an active subprocess.', run_id)
                continue
            interrupt_research_job(db, run_id, previous_status=run['status'])
            logger.warning('Marked research run %d as interrupted (was %s).', run_id, run['status'])
            emit_event('research.recovered', run_id=int(run_id), previous_status=run['status'])
            recovered += 1
    # Clean stale locks
    lock_dir = Path(LOCK_DIR)
    if lock_dir.exists():
        for lf in lock_dir.glob('research_*.lock'):
            try:
                pid = int(lf.read_text().strip())
                os.kill(pid, 0)
            except (ProcessLookupError, ValueError, OSError):
                lf.unlink(missing_ok=True)
                logger.info('Cleaned stale lock: %s', lf)
    # Clean orphaned staging files
    staging_dir = Path(SETTINGS_STAGING_DIR)
    if staging_dir.exists():
        for sf in staging_dir.glob('research_settings_*.json'):
            try:
                rid = int(sf.stem.split('_')[-1])
                if not _has_active_research_subprocess(rid):
                    sf.unlink(missing_ok=True)
            except (ValueError, IndexError):
                sf.unlink(missing_ok=True)
    return recovered


# ---------------------------------------------------------------------------
# Apply actions (quick, run in web process)
# ---------------------------------------------------------------------------

def apply_recommended_calibration(settings: Settings, db: Database, run_id: int) -> Dict[str, object]:
    run = db.get_research_run(run_id)
    if not run:
        raise ValueError('Research run not found.')
    if run['status'] != 'completed' or not run.get('result'):
        raise ValueError('Research run has not completed.')
    calibration = run['result'].get('calibration') or {}
    recommended = calibration.get('recommended') or {}
    weights = recommended.get('weights')
    if not weights:
        raise ValueError('No recommended weights available.')
    if not calibration.get('should_apply'):
        raise ValueError('Calibration did not clear minimum improvement threshold.')
    save_settings_override(settings, _weights_to_override_payload(weights))
    return {'applied': True, 'weights': weights}


def apply_recommended_schedule(settings: Settings, db: Database, run_id: int) -> Dict[str, object]:
    run = db.get_research_run(run_id)
    if not run:
        raise ValueError('Research run not found.')
    if run['status'] != 'completed' or not run.get('result'):
        raise ValueError('Research run has not completed.')
    plan = (run['result'] or {}).get('recommended_live_schedule') or {}
    if not plan or not plan.get('suggested_scheduled_scan_offsets'):
        raise ValueError('No recommended live schedule available.')
    if not _schedule_is_actionable(plan):
        raise ValueError(str(plan.get('schedule_application_reason') or 'Recommended schedule did not clear ladder gates and remains advisory only.'))
    save_settings_override(settings, _schedule_to_override_payload(plan))
    return {'applied': True, 'default_scan_offset_minutes': int(plan['default_scan_offset_minutes']),
            'scheduled_scan_offsets': str(plan['suggested_scheduled_scan_offsets'])}
