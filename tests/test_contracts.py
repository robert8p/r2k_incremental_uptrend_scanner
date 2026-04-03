"""
Contract tests for the R2K scanner.

These tests protect the boundaries that have historically caused breakage:
  1. Settings model loads all required fields with valid defaults
  2. Scan summary payloads match the expected contract
  3. Validation summary structures remain stable
  4. Research result payloads remain stable
  5. Template-expected keys actually exist in their data sources
  6. Candidate payload shape is consistent

Run with:  pytest tests/ -v
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Allow importing app modules when running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# 1. Settings contract
# ---------------------------------------------------------------------------

class TestSettingsContract:
    """Settings model must load cleanly and expose all required fields."""

    def test_settings_loads_with_defaults(self):
        """Settings() with no env should produce valid defaults."""
        from app.config import Settings
        s = Settings()
        assert s.app_name
        assert s.database_path
        assert s.target_pct > 0
        assert s.default_scan_offset_minutes > 0

    def test_settings_weights_sum_to_one(self):
        from app.config import Settings
        s = Settings()
        total = sum(s.weights.values())
        assert abs(total - 1.0) < 0.01, f'Weights sum to {total}, expected ~1.0'

    def test_settings_weights_keys_match_expected(self):
        from app.config import Settings
        s = Settings()
        expected_keys = {
            'target_strength', 'liquidity', 'volatility_capacity',
            'dynamic_range', 'range_position', 'time_feasibility',
            'execution_quality',
        }
        assert set(s.weights.keys()) == expected_keys

    def test_settings_scheduled_offsets_parse(self):
        from app.config import Settings
        s = Settings(scheduled_scan_offsets='90,120,150')
        assert s.scheduled_offsets == [90, 120, 150]

    def test_settings_research_offset_values_parse(self):
        from app.config import Settings
        s = Settings(research_offset_ladder='90,120,150,180')
        assert s.research_offset_values == [90, 120, 150, 180]

    def test_settings_public_snapshot_has_required_keys(self):
        """The public snapshot is used by templates and status pages."""
        from app.config import Settings
        s = Settings()
        snap = s.public_snapshot()

        required_keys = [
            'app_name', 'trading_mode', 'alpaca_data_feed',
            'min_price', 'target_pct', 'stretch_target_pct',
            'default_scan_offset_minutes', 'scheduled_offsets',
            'weights', 'weights_raw', 'research_offset_values',
            'replay_entry_fill_mode', 'replay_target_hit_mode',
        ]
        for key in required_keys:
            assert key in snap, f'Missing key in public_snapshot: {key}'

    def test_settings_no_nan_or_inf_in_defaults(self):
        """No default threshold should be NaN or Inf."""
        from app.config import Settings
        import math
        s = Settings()
        for field_name, field_info in type(s).model_fields.items():
            value = getattr(s, field_name)
            if isinstance(value, float):
                assert not math.isnan(value), f'{field_name} is NaN'
                assert not math.isinf(value), f'{field_name} is Inf'


# ---------------------------------------------------------------------------
# 2. Scan summary contract
# ---------------------------------------------------------------------------

class TestScanSummaryContract:

    def test_scan_summary_validates(self):
        from app.contracts import ScanSummary
        summary = ScanSummary(
            goal='test goal',
            stage1_target_group_count=50,
            stage2_candidate_count=5,
            range_trade_cutoff_rule='rule',
            scan_focus='focus',
        )
        assert summary.stage1_target_group_count == 50

    def test_scan_summary_defaults(self):
        from app.contracts import ScanSummary
        summary = ScanSummary()
        assert summary.stage1_target_group_count == 0
        assert summary.goal == ''


# ---------------------------------------------------------------------------
# 3. Candidate payload contract
# ---------------------------------------------------------------------------

class TestCandidatePayloadContract:

    def test_candidate_payload_full(self):
        from app.contracts import CandidatePayload
        c = CandidatePayload(
            symbol='AAPL',
            mover_rank=1,
            intraday_pct_gain=3.5,
            advanced_to_stage2=True,
            current_price=150.0,
            total_score=72.5,
            recommendation_tier='ready_now',
            recommendation_book='actionable_now',
            component_scores={'dynamic_range': 65.0},
        )
        assert c.symbol == 'AAPL'
        assert c.advanced_to_stage2 is True

    def test_candidate_payload_minimal_rejected(self):
        from app.contracts import CandidatePayload
        c = CandidatePayload(
            symbol='XYZ',
            advanced_to_stage2=False,
            exclusion_reason='Missing data',
        )
        assert c.recommendation_tier == 'rejected'
        assert c.total_score is None

    def test_candidate_payload_hydrates_recommendation_fields_from_metrics(self):
        from app.contracts import CandidatePayload
        c = CandidatePayload(
            symbol='ABC',
            advanced_to_stage2=True,
            metrics={
                'recommendation_tier': 'watchlist',
                'recommendation_book': 'touch_soon_queue',
                'execution_lane': 'monitor_5m',
                'touch_window_band': 'touch_soon',
                'monitor_cadence_minutes': 5,
                'actionability_score': 41.5,
            },
        )
        assert c.recommendation_tier == 'watchlist'
        assert c.recommendation_book == 'touch_soon_queue'
        assert c.execution_lane == 'monitor_5m'
        assert c.touch_window_band == 'touch_soon'
        assert c.monitor_cadence_minutes == 5
        assert c.actionability_score == 41.5


# ---------------------------------------------------------------------------
# 4. Validation summary contract
# ---------------------------------------------------------------------------

class TestValidationSummaryContract:

    def test_validation_summary_from_run16_evidence(self):
        """
        Parse the actual run-16 summary to confirm the contract covers
        all fields the real system produces.
        """
        from app.contracts import ValidationSummary
        evidence_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'evidence', 'run_16', 'summary.json'
        )
        if not os.path.exists(evidence_path):
            pytest.skip('Run-16 evidence not available in this environment.')

        with open(evidence_path) as f:
            raw = json.load(f)

        summary_data = raw.get('summary', {})
        # This should parse without error — any new field in the real
        # output that is NOT in the contract will be captured by extra='allow'
        summary = ValidationSummary(**summary_data)

        assert summary.scored_replay_rows_total == 2259
        assert summary.advanced_stage2_total == 41
        assert summary.precision_at_10 == pytest.approx(0.5625, abs=0.001)
        assert summary.entry_touch_rate_stage2 == pytest.approx(0.6829, abs=0.001)
        assert summary.validation_verdict.verdict == 'REVIEW'
        assert summary.baseline_comparison.mover_rank_only.precision_at_10 == pytest.approx(0.0102, abs=0.001)
        assert summary.entry_methodology.requires_post_scan_touch is True
        assert summary.score_bucket_monotonicity.ok is True

    def test_validation_summary_defaults(self):
        from app.contracts import ValidationSummary
        s = ValidationSummary()
        assert s.scored_replay_rows_total == 0
        assert s.validation_verdict.verdict == 'UNKNOWN'
        assert s.entry_methodology.requires_post_scan_touch is False

    def test_validation_summary_roundtrip_json(self):
        """Contract should survive JSON serialization and deserialization."""
        from app.contracts import ValidationSummary
        original = ValidationSummary(
            days=59,
            scored_replay_rows_total=2259,
            advanced_stage2_total=41,
            precision_at_10=0.5625,
        )
        json_str = original.model_dump_json()
        restored = ValidationSummary.model_validate_json(json_str)
        assert restored.precision_at_10 == original.precision_at_10
        assert restored.days == 59


# ---------------------------------------------------------------------------
# 5. Research result contract
# ---------------------------------------------------------------------------

class TestResearchResultContract:

    def test_research_result_single_validation(self):
        from app.contracts import ResearchResult
        r = ResearchResult(
            validation_id=16,
            best_validation_id=16,
            calibration={'eligible': True},
            auto_applied=False,
        )
        assert r.best_validation_id == 16

    def test_research_result_offset_ladder(self):
        from app.contracts import ResearchResult
        r = ResearchResult(
            best_validation_id=16,
            offset_rows=[
                {'scan_offset_minutes': 90, 'utility_score': 0.45},
                {'scan_offset_minutes': 120, 'utility_score': 0.72},
            ],
            schedule_plan={'default_scan_offset_minutes': 120},
            auto_applied_schedule=True,
        )
        assert len(r.offset_rows) == 2
        assert r.auto_applied_schedule is True

    def test_research_result_runtime_aliases_normalize(self):
        from app.contracts import normalize_research_result
        payload = normalize_research_result({
            'best_validation_id': 16,
            'offset_ladder_summary': [{'scan_offset_minutes': 120, 'utility_score': 0.72}],
            'recommended_live_schedule': {'default_scan_offset_minutes': 120},
        })
        assert payload['offset_rows'][0]['scan_offset_minutes'] == 120
        assert payload['schedule_plan']['default_scan_offset_minutes'] == 120
        assert payload['offset_ladder_summary'][0]['scan_offset_minutes'] == 120
        assert payload['recommended_live_schedule']['default_scan_offset_minutes'] == 120


# ---------------------------------------------------------------------------
# 6. Template key expectations
# ---------------------------------------------------------------------------

class TestTemplateKeyExpectations:
    """
    Verify that keys expected by Jinja templates are present in the
    data structures that feed them. This catches the naming drift
    identified in the external review.
    """

    def test_index_template_keys(self):
        """Index template expects these keys from latest_scan."""
        required_scan_keys = [
            'id', 'trading_day', 'scan_offset_minutes', 'status',
            'stage1_count', 'stage2_count', 'created_at',
        ]
        # These are the column names from the scans table
        from app.db import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            conn = db.connect()
            cursor = conn.execute('SELECT * FROM scans LIMIT 0')
            col_names = [desc[0] for desc in cursor.description]
            conn.close()
        for key in required_scan_keys:
            assert key in col_names, f'Scans table missing column: {key}'

    def test_scan_candidate_table_columns(self):
        """Candidate detail template expects these columns."""
        required = [
            'scan_id', 'symbol', 'mover_rank', 'advanced_to_stage2',
            'total_score', 'current_price', 'entry_low', 'entry_high',
            'target_price', 'stop_price', 'rationale',
        ]
        from app.db import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            conn = db.connect()
            cursor = conn.execute('SELECT * FROM scan_candidates LIMIT 0')
            col_names = [desc[0] for desc in cursor.description]
            conn.close()
        for key in required:
            assert key in col_names, f'scan_candidates table missing column: {key}'


# ---------------------------------------------------------------------------
# 7. DB round-trip integrity
# ---------------------------------------------------------------------------

class TestDBRoundTrip:

    def test_scan_insert_and_retrieve(self):
        from app.db import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            scan = {
                'created_at': '2026-01-01T00:00:00',
                'trading_day': '2026-01-01',
                'scan_offset_minutes': 120,
                'scan_timestamp': '2026-01-01T11:30:00',
                'status': 'ok',
                'mode': 'scan_only',
                'universe_count': 1800,
                'stage1_count': 50,
                'stage2_count': 5,
                'summary': {'goal': 'test'},
            }
            candidates = [{
                'symbol': 'TEST',
                'company_name': 'Test Corp',
                'mover_rank': 1,
                'intraday_pct_gain': 3.5,
                'advanced_to_stage2': True,
                'exclusion_reason': None,
                'current_price': 10.0,
                'current_cum_volume': 50000,
                'relative_volume': 2.1,
                'total_score': 72.5,
                'component_scores': {'dynamic_range': 65.0},
                'metrics': {'range_classification_code': 'A'},
                'rationale': 'test rationale',
                'entry_low': 9.80,
                'entry_high': 9.95,
                'target_price': 10.10,
                'stretch_target_price': 10.20,
                'stop_price': 9.60,
                'chart_context': {'band_low': 9.70},
            }]
            scan_id = db.insert_scan(scan, candidates)
            assert scan_id > 0

            retrieved = db.get_scan(scan_id)
            assert retrieved is not None
            assert retrieved['trading_day'] == '2026-01-01'
            assert retrieved['summary']['goal'] == 'test'

            cands = db.get_scan_candidates(scan_id)
            assert len(cands) == 1
            assert cands[0]['symbol'] == 'TEST'
            assert cands[0]['component_scores']['dynamic_range'] == 65.0


    def test_scan_insert_normalizes_summary_and_candidate_contracts(self):
        from app.db import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            scan_id = db.insert_scan(
                {
                    'created_at': '2026-01-01T00:00:00',
                    'trading_day': '2026-01-01',
                    'scan_offset_minutes': 120,
                    'scan_timestamp': '2026-01-01T11:30:00',
                    'status': 'ok',
                    'mode': 'scan_only',
                    'universe_count': 1800,
                    'stage1_count': 50,
                    'stage2_count': 1,
                    'summary': {'goal': 'test'},
                },
                [{
                    'symbol': 'TEST',
                    'advanced_to_stage2': False,
                    'exclusion_reason': 'test',
                }],
            )
            scan = db.get_scan(scan_id)
            candidate = db.get_candidate(scan_id, 'TEST')
            assert scan['summary']['target_group_size'] == 0
            assert 'data_contract' in scan['summary']
            assert candidate['recommendation_tier'] == 'rejected'
            assert candidate['execution_lane'] == 'passive_watchlist'
            assert candidate['chart_context'] == {}

    def test_research_result_roundtrip_normalizes_aliases(self):
        from app.db import Database
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            run_id = db.insert_research_run({'created_at': '2026-01-01T00:00:00', 'mode': 'test'})
            db.update_research_run(
                run_id,
                status='completed',
                result={
                    'best_validation_id': 16,
                    'offset_ladder_summary': [{'scan_offset_minutes': 120, 'utility_score': 0.72}],
                    'recommended_live_schedule': {'default_scan_offset_minutes': 120},
                },
            )
            run = db.get_research_run(run_id)
            assert run['result']['offset_rows'][0]['scan_offset_minutes'] == 120
            assert run['result']['schedule_plan']['default_scan_offset_minutes'] == 120
            assert run['result']['recommended_live_schedule']['default_scan_offset_minutes'] == 120



class TestDiagnosticsContractHealth:

    def test_contract_health_reports_ok_on_empty_db(self):
        from app.db import Database
        from app.services.diagnostics import build_contract_health
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(os.path.join(tmpdir, 'test.db'))
            report = build_contract_health(db)
            assert report['ok'] is True
            assert report['errors'] == []



class TestViewModels:

    def test_build_scan_view_backfills_template_fields(self):
        from app.view_models import build_scan_view
        scan = build_scan_view({
            'id': 1,
            'stage1_count': 50,
            'stage2_count': 5,
            'universe_count': 1800,
            'summary': {'goal': 'test'},
        }, alpaca_data_feed='iex')
        assert scan['summary']['target_group_size'] == 50
        assert scan['summary']['advanced_count'] == 5
        assert scan['summary']['data_contract']['alpaca_data_feed'] == 'iex'

    def test_build_candidate_view_backfills_metric_defaults(self):
        from app.view_models import build_candidate_view
        candidate = build_candidate_view({'symbol': 'TEST', 'advanced_to_stage2': False, 'exclusion_reason': 'test'})
        assert candidate['metrics']['range_classification'] == 'Unknown'
        assert 'distance_to_entry_pct' in candidate['metrics']
        assert candidate['chart_context'] == {}

    def test_build_research_view_shapes_runtime_rows(self):
        from app.view_models import build_research_view
        run = build_research_view({
            'id': 7,
            'status': 'completed',
            'params': {'start_date': '2026-01-01', 'end_date': '2026-03-01'},
            'result': {
                'best_validation_id': 16,
                'offset_rows': [{'scan_offset_minutes': 120, 'advanced_rows': 41, 'conditional_precision_at_10': 0.5}],
                'schedule_plan': {'default_scan_offset_minutes': 120},
            },
        })
        row = run['result']['offset_ladder_summary'][0]
        assert row['advanced_stage2_total'] == 41
        assert row['conditional_precision_at_10_entry_touched'] == 0.5
        assert run['result']['recommended_live_schedule']['default_scan_offset_minutes'] == 120



def test_normalize_scan_summary_preserves_shortlist_alignment():
    from app.contracts import normalize_scan_summary

    normalized = normalize_scan_summary({
        'goal': 'g',
        'shortlist_alignment': {
            'selection_mode': 'aligned_prefilter_pool',
            'alignment_pool_size': 150,
            'alignment_prefilter_kept_count': 34,
            'prefilter_rejection_counts': {'Average daily dollar volume below threshold.': 116},
        },
    })

    assert normalized['shortlist_alignment']['selection_mode'] == 'aligned_prefilter_pool'
    assert normalized['shortlist_alignment']['alignment_pool_size'] == 150
