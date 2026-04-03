from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field

from app.config import Settings
from app.contracts import ValidationSummary, normalize_validation_summary
from app.db import Database
from app.services.alpaca_client import AlpacaClient
from app.services.telemetry import emit_event

ProgressCallback = Optional[Callable[[float, str], None]]


class ValidationRunRequest(BaseModel):
    start_date: str
    end_date: str
    scan_offset_minutes: int
    cache_history: bool = False
    persist: bool = True


class ValidationRunResult(BaseModel):
    id: int
    created_at: str = ''
    start_date: str
    end_date: str
    scan_offset_minutes: int
    status: str = 'ok'
    summary: ValidationSummary
    rows: List[Dict[str, Any]] = Field(default_factory=list)


def _baseline_precision_at_10(summary: Dict[str, Any]) -> float:
    baseline = (summary.get('baseline_comparison') or {}).get('mover_rank_only') or {}
    try:
        return float(baseline.get('precision_at_10') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _run_validation_impl(*args, **kwargs):
    from app.services.backtest import run_validation

    return run_validation(*args, **kwargs)


def _calibrate_rows_impl(*args, **kwargs):
    from app.services.calibration import calibrate_rows

    return calibrate_rows(*args, **kwargs)


def execute_validation_run(
    settings: Settings,
    db: Database,
    alpaca: AlpacaClient,
    request: ValidationRunRequest,
    *,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    emit_event(
        'validation.started',
        start_date=request.start_date,
        end_date=request.end_date,
        scan_offset_minutes=int(request.scan_offset_minutes),
        cache_history=bool(request.cache_history),
    )
    try:
        payload = _run_validation_impl(
            settings,
            db,
            alpaca,
            start_date=request.start_date,
            end_date=request.end_date,
            scan_offset_minutes=int(request.scan_offset_minutes),
            progress_callback=progress_callback,
            cache_history=bool(request.cache_history),
            persist=bool(request.persist),
        )
        normalized = dict(payload)
        normalized['summary'] = normalize_validation_summary(normalized.get('summary'))
        result = ValidationRunResult(**normalized).model_dump(exclude_none=True)
        summary = result.get('summary') or {}
        emit_event(
            'validation.completed',
            validation_id=int(result['id']),
            start_date=result['start_date'],
            end_date=result['end_date'],
            scan_offset_minutes=int(result['scan_offset_minutes']),
            days=int(summary.get('days') or 0),
            advanced_stage2_total=int(summary.get('advanced_stage2_total') or 0),
            precision_at_10=float(summary.get('precision_at_10') or 0.0),
            row_count=len(result.get('rows') or []),
        )
        return result
    except Exception as exc:
        emit_event(
            'validation.failed',
            level='error',
            start_date=request.start_date,
            end_date=request.end_date,
            scan_offset_minutes=int(request.scan_offset_minutes),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise


def execute_validation_and_calibration(
    settings: Settings,
    db: Database,
    alpaca: AlpacaClient,
    request: ValidationRunRequest,
    *,
    progress_callback: ProgressCallback = None,
) -> Dict[str, Any]:
    validation = execute_validation_run(
        settings,
        db,
        alpaca,
        request,
        progress_callback=progress_callback,
    )
    summary = dict(validation.get('summary') or {})
    try:
        calibration = _calibrate_rows_impl(
            validation['rows'],
            settings.weights,
            settings.calibration_min_improvement,
            mover_rank_baseline_precision_at_10=_baseline_precision_at_10(summary),
        )
        summary['calibration'] = calibration
        validation['summary'] = summary
        validation = ValidationRunResult(**validation).model_dump(exclude_none=True)
        emit_event(
            'calibration.evaluated',
            validation_id=int(validation['id']),
            eligible=bool(calibration.get('eligible')),
            should_apply=bool(calibration.get('should_apply')),
            improvement=float(calibration.get('improvement') or 0.0),
        )
        return {
            'validation': validation,
            'calibration': calibration,
        }
    except Exception as exc:
        emit_event(
            'calibration.failed',
            level='error',
            validation_id=int(validation['id']),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
