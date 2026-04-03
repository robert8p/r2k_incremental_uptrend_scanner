from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, Optional

import pandas as pd

from app.config import Settings
from app.db import Database
from app.repositories import RepositoryBundle, ensure_repository_bundle
from app.view_models import (
    build_candidate_list,
    build_candidate_view,
    build_research_list,
    build_research_view,
    build_scan_view,
    build_validation_list,
    build_validation_view,
)
from app.services.live_trust import build_live_trust_snapshot
from app.services.checkpoint_decision_surface import build_checkpoint_decision_surface
from app.services.decision_bundle import read_cached_decision_state
from app.services.goal_alignment import build_goal_alignment_summary


ChartBuilder = Callable[[Dict[str, Any]], str]


def build_index_page_context(settings: Settings, db: Database | RepositoryBundle) -> Dict[str, Any]:
    repos = ensure_repository_bundle(db)
    latest_scan = build_scan_view(repos.scan.get_latest(), alpaca_data_feed=settings.alpaca_data_feed)
    latest_candidates = build_candidate_list(repos.scan.get_candidates(latest_scan['id'])) if latest_scan else []
    recent_validations = build_validation_list(repos.validation.list_recent(limit=5))
    session_info = None
    try:
        from app.services.market_time import get_session_for_day, latest_or_previous_trading_day
        session_info = get_session_for_day(latest_or_previous_trading_day(), settings.default_scan_offset_minutes)
    except Exception:
        session_info = None
    checkpoint_review = build_checkpoint_decision_surface(
        settings,
        repos,
        trading_day=(latest_scan or {}).get('trading_day'),
        offsets=[120, 150],
    )
    decision_state = read_cached_decision_state(settings)
    try:
        from app.services.universe import load_universe
        universe_status = load_universe(settings, repos.db, force_refresh=False)['status']
    except Exception:
        universe_status = {}
    return {
        'latest_scan': latest_scan,
        'latest_candidates': latest_candidates,
        'recent_validations': recent_validations,
        'session_info': session_info,
        'live_trust': build_live_trust_snapshot(settings, repos.db),
        'checkpoint_review': checkpoint_review,
        'decision_state': decision_state,
        'goal_alignment': build_goal_alignment_summary(settings, universe_status=universe_status, decision_state=decision_state),
    }



def build_scan_detail_page_context(settings: Settings, db: Database | RepositoryBundle, scan_id: int) -> Optional[Dict[str, Any]]:
    repos = ensure_repository_bundle(db)
    scan = build_scan_view(repos.scan.get(scan_id), alpaca_data_feed=settings.alpaca_data_feed)
    if not scan:
        return None
    candidates = build_candidate_list(repos.scan.get_candidates(scan_id))
    stage2 = [c for c in candidates if c['advanced_to_stage2']]
    excluded = [c for c in candidates if not c['advanced_to_stage2']]
    tier_counts = Counter(str(candidate.get('recommendation_tier') or 'unknown') for candidate in candidates)
    book_counts = Counter(str(candidate.get('recommendation_book') or 'unknown') for candidate in candidates)
    lane_counts = Counter(str(candidate.get('execution_lane') or 'unknown') for candidate in candidates)
    decision_surface = {
        'advanced_stage2_count': len(stage2),
        'actionable_tier_count': sum(1 for candidate in candidates if str(candidate.get('recommendation_tier') or '') in {'headline_shortlist', 'ready_now', 'near_ready'}),
        'watchlist_tier_count': tier_counts.get('watchlist', 0),
        'rejected_tier_count': tier_counts.get('rejected', 0),
        'headline_shortlist_count': tier_counts.get('headline_shortlist', 0),
        'ready_now_count': tier_counts.get('ready_now', 0),
        'near_ready_count': tier_counts.get('near_ready', 0),
        'touch_soon_queue_count': book_counts.get('touch_soon_queue', 0),
        'touch_later_queue_count': book_counts.get('touch_later_queue', 0),
        'structural_watchlist_count': book_counts.get('structural_watchlist', 0),
        'rejected_book_count': book_counts.get('rejected', 0),
        'monitor_5m_lane_count': lane_counts.get('monitor_5m', 0),
        'passive_watchlist_lane_count': lane_counts.get('passive_watchlist', 0),
    }
    checkpoint_review = build_checkpoint_decision_surface(settings, repos, trading_day=scan.get('trading_day'), offsets=[120, 150])
    return {
        'scan': scan,
        'candidates': candidates,
        'stage2': stage2,
        'excluded': excluded,
        'decision_surface': decision_surface,
        'checkpoint_review': checkpoint_review,
    }



def build_candidate_detail_page_context(
    settings: Settings,
    db: Database | RepositoryBundle,
    scan_id: int,
    symbol: str,
    *,
    chart_html: str = '',
) -> Optional[Dict[str, Any]]:
    repos = ensure_repository_bundle(db)
    scan = build_scan_view(repos.scan.get(scan_id), alpaca_data_feed=settings.alpaca_data_feed)
    candidate = build_candidate_view(repos.scan.get_candidate(scan_id, symbol))
    if not scan or not candidate:
        return None
    return {
        'scan': scan,
        'candidate': candidate,
        'chart_html': chart_html,
    }



def build_validation_page_context(settings: Settings, db: Database | RepositoryBundle) -> Dict[str, Any]:
    repos = ensure_repository_bundle(db)
    validations = build_validation_list(repos.validation.list_recent(limit=10))
    research_runs = build_research_list(repos.research.list_recent(limit=10))
    try:
        from app.services.market_time import latest_or_previous_trading_day
        default_end = latest_or_previous_trading_day()
    except Exception:
        default_end = (validations[0]['end_date'] if validations else pd.Timestamp.utcnow().strftime('%Y-%m-%d'))
    default_start = (pd.Timestamp(default_end) - pd.Timedelta(days=settings.default_validation_lookback_days * 2)).strftime('%Y-%m-%d')
    return {
        'validations': validations,
        'selected': validations[0] if validations else None,
        'research_runs': research_runs,
        'selected_research': research_runs[0] if research_runs else None,
        'default_start': default_start,
        'default_end': default_end,
        'default_research_end': default_end,
        'research_offset_values': settings.research_offset_values,
        'gate_audit_defaults': {
            'scenario_a_name': 'liquidity_relaxed',
            'scenario_a_min_avg_dollar_volume': 1500000,
            'scenario_a_min_price': settings.min_price,
            'scenario_a_low_price_hard_floor': settings.low_price_hard_floor,
            'scenario_b_name': 'liquidity_and_price_relaxed',
            'scenario_b_min_avg_dollar_volume': 1000000,
            'scenario_b_min_price': max(0.5, float(settings.min_price) - 0.5),
            'scenario_b_low_price_hard_floor': max(0.25, float(settings.low_price_hard_floor) - 0.25),
        },
        'goal_seek_defaults': {
            'start_date': (pd.Timestamp(default_end) - pd.Timedelta(days=260)).strftime('%Y-%m-%d'),
            'end_date': default_end,
            'train_days': 60,
            'test_days': 20,
            'step_days': 20,
            'embargo_days': 1,
            'offsets': '120,150',
            'config_scope': 'focused_liquidity',
        }
    }



def build_validation_detail_page_context(
    settings: Settings,
    db: Database | RepositoryBundle,
    validation_id: int,
    *,
    chart_html_builder: Optional[ChartBuilder] = None,
) -> Optional[Dict[str, Any]]:
    repos = ensure_repository_bundle(db)
    validation = build_validation_view(repos.validation.get(validation_id))
    if not validation:
        return None
    return {
        'validations': build_validation_list(repos.validation.list_recent(limit=10)),
        'selected': validation,
        'chart_html': chart_html_builder(validation['summary']) if chart_html_builder else None,
        'default_start': validation['start_date'],
        'default_end': validation['end_date'],
        'default_research_end': validation['end_date'],
        'research_runs': build_research_list(repos.research.list_recent(limit=10)),
        'selected_research': None,
        'research_offset_values': settings.research_offset_values,
        'gate_audit_defaults': {
            'scenario_a_name': 'liquidity_relaxed',
            'scenario_a_min_avg_dollar_volume': 1500000,
            'scenario_a_min_price': settings.min_price,
            'scenario_a_low_price_hard_floor': settings.low_price_hard_floor,
            'scenario_b_name': 'liquidity_and_price_relaxed',
            'scenario_b_min_avg_dollar_volume': 1000000,
            'scenario_b_min_price': max(0.5, float(settings.min_price) - 0.5),
            'scenario_b_low_price_hard_floor': max(0.25, float(settings.low_price_hard_floor) - 0.25),
        },
        'goal_seek_defaults': {
            'start_date': (pd.Timestamp(validation['end_date']) - pd.Timedelta(days=260)).strftime('%Y-%m-%d'),
            'end_date': validation['end_date'],
            'train_days': 60,
            'test_days': 20,
            'step_days': 20,
            'embargo_days': 1,
            'offsets': '120,150',
            'config_scope': 'focused_liquidity',
        }
    }



def build_research_detail_page_context(
    settings: Settings,
    db: Database | RepositoryBundle,
    run_id: int,
    *,
    chart_html_builder: Optional[ChartBuilder] = None,
) -> Optional[Dict[str, Any]]:
    repos = ensure_repository_bundle(db)
    research = build_research_view(repos.research.get(run_id))
    if not research:
        return None
    selected_validation = None
    chart_html = None
    validation_id = ((research.get('result') or {}).get('best_validation_id') or (research.get('result') or {}).get('validation_id')) if research.get('result') else None
    if validation_id:
        selected_validation = build_validation_view(repos.validation.get(int(validation_id)))
        if selected_validation:
            calibration = (research.get('result') or {}).get('calibration')
            if calibration:
                selected_validation['summary']['calibration'] = calibration
            chart_html = chart_html_builder(selected_validation['summary']) if chart_html_builder else None
    return {
        'validations': build_validation_list(repos.validation.list_recent(limit=10)),
        'selected': selected_validation,
        'chart_html': chart_html,
        'default_start': research['params']['start_date'],
        'default_end': research['params']['end_date'],
        'default_research_end': research['params']['end_date'],
        'research_runs': build_research_list(repos.research.list_recent(limit=10)),
        'selected_research': research,
        'research_offset_values': settings.research_offset_values,
        'gate_audit_defaults': {
            'scenario_a_name': 'liquidity_relaxed',
            'scenario_a_min_avg_dollar_volume': 1500000,
            'scenario_a_min_price': settings.min_price,
            'scenario_a_low_price_hard_floor': settings.low_price_hard_floor,
            'scenario_b_name': 'liquidity_and_price_relaxed',
            'scenario_b_min_avg_dollar_volume': 1000000,
            'scenario_b_min_price': max(0.5, float(settings.min_price) - 0.5),
            'scenario_b_low_price_hard_floor': max(0.25, float(settings.low_price_hard_floor) - 0.25),
        },
        'goal_seek_defaults': {
            'start_date': (pd.Timestamp(research['params']['end_date']) - pd.Timedelta(days=260)).strftime('%Y-%m-%d'),
            'end_date': research['params']['end_date'],
            'train_days': 60,
            'test_days': 20,
            'step_days': 20,
            'embargo_days': 1,
            'offsets': '120,150',
            'config_scope': 'focused_liquidity',
        }
    }
