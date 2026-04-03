from __future__ import annotations

import os
import tempfile

from app.config import Settings
from app.db import Database
from app.page_contexts import (
    build_candidate_detail_page_context,
    build_research_detail_page_context,
    build_scan_detail_page_context,
    build_validation_detail_page_context,
    build_validation_page_context,
)


def _settings() -> Settings:
    return Settings()




def test_index_page_context_includes_checkpoint_review():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        db.insert_scan(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'trading_day': '2026-01-01',
                'scan_offset_minutes': 150,
                'scan_timestamp': '2026-01-01T12:00:00+00:00',
                'status': 'ok',
                'mode': 'scan_only',
                'universe_count': 1800,
                'stage1_count': 1,
                'stage2_count': 0,
                'summary': {'goal': 'test'},
            },
            [
                {'symbol': 'AAA', 'advanced_to_stage2': False, 'metrics': {'recommendation_tier': 'rejected'}},
            ],
        )
        from app.page_contexts import build_index_page_context
        context = build_index_page_context(settings, db)
        assert 'checkpoint_review' in context
        assert context['checkpoint_review']['summary']['selected_day'] == '2026-01-01'

def test_scan_detail_page_context_splits_stage2_and_excluded():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        scan_id = db.insert_scan(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'trading_day': '2026-01-01',
                'scan_offset_minutes': 120,
                'scan_timestamp': '2026-01-01T11:30:00+00:00',
                'status': 'ok',
                'mode': 'scan_only',
                'universe_count': 1800,
                'stage1_count': 2,
                'stage2_count': 1,
                'summary': {'goal': 'test'},
            },
            [
                {'symbol': 'AAA', 'advanced_to_stage2': True, 'total_score': 70.0, 'metrics': {'recommendation_tier': 'watchlist', 'recommendation_book': 'touch_soon_queue', 'execution_lane': 'monitor_5m'}},
                {'symbol': 'BBB', 'advanced_to_stage2': False, 'exclusion_reason': 'test', 'metrics': {'recommendation_tier': 'rejected', 'recommendation_book': 'rejected', 'execution_lane': 'passive_watchlist'}},
            ],
        )
        context = build_scan_detail_page_context(settings, db, scan_id)
        assert context is not None
        assert len(context['stage2']) == 1
        assert len(context['excluded']) == 1
        assert context['decision_surface']['advanced_stage2_count'] == 1
        assert context['decision_surface']['watchlist_tier_count'] == 1
        assert context['decision_surface']['rejected_tier_count'] == 1
        assert context['checkpoint_review']['summary']['selected_day'] == '2026-01-01'


def test_candidate_detail_page_context_includes_chart_html():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        scan_id = db.insert_scan(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'trading_day': '2026-01-01',
                'scan_offset_minutes': 120,
                'scan_timestamp': '2026-01-01T11:30:00+00:00',
                'status': 'ok',
                'mode': 'scan_only',
                'universe_count': 1800,
                'stage1_count': 1,
                'stage2_count': 1,
                'summary': {'goal': 'test'},
            },
            [
                {'symbol': 'AAA', 'advanced_to_stage2': True, 'total_score': 70.0},
            ],
        )
        context = build_candidate_detail_page_context(settings, db, scan_id, 'AAA', chart_html='<div>chart</div>')
        assert context is not None
        assert context['chart_html'] == '<div>chart</div>'
        assert context['candidate']['symbol'] == 'AAA'


def test_validation_page_context_defaults_to_latest_items():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        db.insert_validation_run(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'start_date': '2025-12-01',
                'end_date': '2025-12-31',
                'scan_offset_minutes': 120,
                'status': 'ok',
                'summary': {'days': 20, 'precision_at_5': 0.4},
            },
            [],
        )
        run_id = db.insert_research_run({'created_at': '2026-01-01T00:00:00+00:00', 'start_date': '2025-12-01', 'end_date': '2025-12-31'})
        db.update_research_run(run_id, status='completed', result={'validation_id': 1})
        context = build_validation_page_context(settings, db)
        assert context['selected'] is not None
        assert context['selected_research'] is not None


def test_validation_detail_page_context_uses_chart_builder():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        validation_id = db.insert_validation_run(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'start_date': '2025-12-01',
                'end_date': '2025-12-31',
                'scan_offset_minutes': 120,
                'status': 'ok',
                'summary': {'days': 20, 'precision_at_10': 0.5},
            },
            [],
        )
        context = build_validation_detail_page_context(settings, db, validation_id, chart_html_builder=lambda summary: f"p10={summary['precision_at_10']}")
        assert context is not None
        assert context['chart_html'] == 'p10=0.5'


def test_research_detail_page_context_links_validation_and_calibration():
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(os.path.join(tmpdir, 'test.db'))
        validation_id = db.insert_validation_run(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'start_date': '2025-12-01',
                'end_date': '2025-12-31',
                'scan_offset_minutes': 120,
                'status': 'ok',
                'summary': {'days': 20, 'precision_at_10': 0.5},
            },
            [],
        )
        run_id = db.insert_research_run(
            {
                'created_at': '2026-01-01T00:00:00+00:00',
                'start_date': '2025-12-01',
                'end_date': '2025-12-31',
                'scan_offset_minutes': 120,
            }
        )
        db.update_research_run(
            run_id,
            status='completed',
            result={
                'best_validation_id': validation_id,
                'calibration': {'eligible': True},
            },
        )
        context = build_research_detail_page_context(settings, db, run_id, chart_html_builder=lambda summary: 'chart')
        assert context is not None
        assert context['selected']['id'] == validation_id
        assert context['selected']['summary']['calibration']['eligible'] is True
        assert context['chart_html'] == 'chart'


def test_index_page_context_includes_decision_state_from_cache(monkeypatch):
    settings = _settings()
    with tempfile.TemporaryDirectory() as tmpdir:
        settings = Settings(database_path=os.path.join(tmpdir, 'test.db'), data_dir=tmpdir)
        db = Database(os.path.join(tmpdir, 'test.db'))
        monkeypatch.setattr('app.page_contexts.read_cached_decision_state', lambda settings: {'overall_promotion_readiness': 'shadow_profile_promising_but_early'})
        from app.page_contexts import build_index_page_context
        context = build_index_page_context(settings, db)
        assert context['decision_state']['overall_promotion_readiness'] == 'shadow_profile_promising_but_early'
