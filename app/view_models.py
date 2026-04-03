from __future__ import annotations

from typing import Any, Dict, List

from app.contracts import (
    normalize_candidate_payload,
    normalize_research_result,
    normalize_scan_summary,
    normalize_validation_summary,
)


def build_scan_view(scan: Dict[str, Any] | None, *, alpaca_data_feed: str, universe_source: str = 'IWM holdings proxy') -> Dict[str, Any] | None:
    if not scan:
        return None
    shaped = dict(scan)
    summary = normalize_scan_summary(shaped.get('summary'))
    if not summary.get('target_group_size') and shaped.get('stage1_count') is not None:
        summary['target_group_size'] = shaped.get('stage1_count', 0)
    if not summary.get('stage1_target_group_count') and shaped.get('stage1_count') is not None:
        summary['stage1_target_group_count'] = shaped.get('stage1_count', 0)
    if not summary.get('advanced_count') and shaped.get('stage2_count') is not None:
        summary['advanced_count'] = shaped.get('stage2_count', 0)
    if not summary.get('stage2_candidate_count') and shaped.get('stage2_count') is not None:
        summary['stage2_candidate_count'] = shaped.get('stage2_count', 0)
    summary.setdefault('leader_symbol', None)
    summary.setdefault('leader_gain_pct', None)
    summary.setdefault('checkpoint_minutes_until_close', None)
    data_contract = dict(summary.get('data_contract') or {})
    data_contract.setdefault('alpaca_data_feed', alpaca_data_feed)
    data_contract.setdefault('universe_source', universe_source)
    data_contract.setdefault('universe_count', shaped.get('universe_count'))
    summary['data_contract'] = data_contract
    shaped['summary'] = summary
    return shaped


def build_candidate_view(candidate: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not candidate:
        return None
    shaped = normalize_candidate_payload(candidate)
    metrics = dict(shaped.get('metrics') or {})
    metrics.setdefault('range_classification', 'Unknown')
    metrics.setdefault('range_band_low', None)
    metrics.setdefault('range_band_high', None)
    metrics.setdefault('range_band_width_pct', None)
    metrics.setdefault('range_current_location', None)
    metrics.setdefault('distance_to_entry_pct', None)
    shaped['metrics'] = metrics
    shaped['component_scores'] = dict(shaped.get('component_scores') or {})
    shaped['chart_context'] = dict(shaped.get('chart_context') or {})
    return shaped



def build_validation_view(validation: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not validation:
        return None
    shaped = dict(validation)
    shaped['summary'] = normalize_validation_summary(shaped.get('summary'))
    return shaped



def _shape_offset_row(row: Dict[str, Any]) -> Dict[str, Any]:
    shaped = dict(row or {})
    shaped.setdefault('advanced_stage2_total', shaped.get('advanced_rows', 0))
    shaped.setdefault('conditional_precision_at_10_entry_touched', shaped.get('conditional_precision_at_10', 0.0))
    shaped.setdefault('entry_touch_rate_stage2', shaped.get('entry_touch_rate', 0.0))
    shaped.setdefault('validation_verdict', shaped.get('validation_verdict', 'UNKNOWN'))
    shaped.setdefault('recommended', False)
    return shaped



def build_research_view(run: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not run:
        return None
    shaped = dict(run)
    params = dict(shaped.get('params') or {})
    params.setdefault('start_date', '')
    params.setdefault('end_date', '')
    params.setdefault('scan_offset_minutes', 0)
    params.setdefault('run_offset_ladder', False)
    params.setdefault('offset_ladder', [])
    shaped['params'] = params

    result = shaped.get('result')
    if result is not None:
        normalized = normalize_research_result(result)
        normalized['offset_ladder_summary'] = [
            _shape_offset_row(row) for row in (normalized.get('offset_ladder_summary') or [])
        ]
        normalized['offset_rows'] = normalized.get('offset_rows') or normalized['offset_ladder_summary']
        normalized['recommended_live_schedule'] = dict(normalized.get('recommended_live_schedule') or {})
        normalized['schedule_plan'] = normalized.get('schedule_plan') or normalized['recommended_live_schedule']
        shaped['result'] = normalized
    else:
        shaped['result'] = None
    return shaped



def build_candidate_list(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [build_candidate_view(candidate) for candidate in candidates if candidate is not None]



def build_validation_list(validations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [build_validation_view(validation) for validation in validations if validation is not None]



def build_research_list(research_runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [build_research_view(run) for run in research_runs if run is not None]
