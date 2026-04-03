from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.db import Database
from app.services.research import _recommend_live_schedule, _schedule_is_actionable, apply_recommended_schedule


def test_recommend_live_schedule_marks_fallback_as_advisory_only():
    settings = Settings(ladder_min_precision_at_10=0.6, ladder_min_advanced_rows=30)
    plan = _recommend_live_schedule([
        {
            'scan_offset_minutes': 120,
            'validation_id': 1,
            'utility_score': 0.7,
            'precision_at_10': 0.5,
            'advanced_stage2_total': 12,
            'clears_quality_gate': False,
            'clears_sample_gate': False,
        }
    ], settings)

    assert plan['all_offsets_failed_gates'] is True
    assert plan['schedule_should_apply'] is False
    assert _schedule_is_actionable(plan) is False


def test_recommend_live_schedule_marks_eligible_plan_actionable():
    settings = Settings(ladder_min_precision_at_10=0.5, ladder_min_advanced_rows=20)
    plan = _recommend_live_schedule([
        {
            'scan_offset_minutes': 120,
            'validation_id': 1,
            'utility_score': 0.7,
            'precision_at_10': 0.6,
            'advanced_stage2_total': 35,
            'clears_quality_gate': True,
            'clears_sample_gate': True,
        }
    ], settings)

    assert plan['all_offsets_failed_gates'] is False
    assert plan['schedule_should_apply'] is True
    assert _schedule_is_actionable(plan) is True


def test_apply_recommended_schedule_refuses_advisory_only_plan(tmp_path):
    settings = Settings(settings_override_path=str(tmp_path / 'override.json'))
    db = Database(str(tmp_path / 'test.db'))
    run_id = db.insert_research_run(
        {'created_at': datetime.now(timezone.utc).isoformat(), 'mode': 'test'},
        status='completed',
        message='done',
    )
    db.update_research_run(
        run_id,
        status='completed',
        result={
            'recommended_live_schedule': {
                'default_scan_offset_minutes': 120,
                'suggested_scheduled_scan_offsets': '120',
                'all_offsets_failed_gates': True,
                'schedule_should_apply': False,
                'schedule_application_reason': 'Advisory only.',
            }
        },
    )

    with pytest.raises(ValueError, match='Advisory only'):
        apply_recommended_schedule(settings, db, run_id)
