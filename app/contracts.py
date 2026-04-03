"""
Typed boundary contracts for the R2K scanner.

These Pydantic models enforce the shape of data flowing between layers:
  - scan summaries
  - candidate payloads
  - validation summaries
  - research results

They replace the current implicit-dict approach that has caused field drift
and template breakage across versions. Each model should be used:
  1. When writing to the DB (validate before persisting)
  2. When reading from the DB (parse stored JSON into typed model)
  3. When passing data to templates (template gets model attributes)

These models are the single source of truth for payload structure.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Scan contracts
# ---------------------------------------------------------------------------

class ScanSummary(BaseModel):
    """Summary payload stored in scans.summary_json."""
    goal: str = ''
    target_group_size: int = 0
    stage1_target_group_count: int = 0
    advanced_count: int = 0
    stage2_candidate_count: int = 0
    leader_symbol: Optional[str] = None
    leader_gain_pct: Optional[float] = None
    checkpoint_minutes_until_close: Optional[int] = None
    scan_offset_minutes: Optional[int] = None
    data_contract: Dict[str, Any] = Field(default_factory=dict)
    shortlist_alignment: Dict[str, Any] = Field(default_factory=dict)
    range_trade_cutoff_rule: str = ''
    scan_focus: str = ''


class ChartContext(BaseModel):
    band_low: Optional[float] = None
    band_high: Optional[float] = None
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    target_price: Optional[float] = None
    stop_price: Optional[float] = None


class CandidatePayload(BaseModel):
    """Shape of a scored candidate row from build_candidate_score()."""

    @model_validator(mode='before')
    @classmethod
    def _hydrate_top_level_aliases_from_metrics(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        metrics = dict(data.get('metrics') or {})
        alias_fields = {
            'recommendation_tier': 'recommendation_tier',
            'recommendation_book': 'recommendation_book',
            'execution_lane': 'execution_lane',
            'monitor_cadence_minutes': 'monitor_cadence_minutes',
            'touch_window_band': 'touch_window_band',
            'structural_score': 'structural_score',
            'entry_touch_likelihood_score': 'entry_touch_likelihood_score',
            'touch_urgency_score': 'touch_urgency_score',
            'expected_minutes_to_touch': 'expected_minutes_to_touch',
            'follow_through_confidence_score': 'follow_through_confidence_score',
            'expected_actionability_score': 'expected_actionability_score',
            'actionability_score': 'actionability_score',
            'headline_rank_score': 'headline_rank_score',
            'pullback_queue_rank_score': 'pullback_queue_rank_score',
            'queue_actionability_score': 'queue_actionability_score',
            'queue_escalation_score': 'queue_escalation_score',
            'queue_priority_score': 'queue_priority_score',
        }
        for top_level_key, metric_key in alias_fields.items():
            if (top_level_key not in data or data.get(top_level_key) is None) and metric_key in metrics and metrics.get(metric_key) is not None:
                data[top_level_key] = metrics.get(metric_key)
        if ('exclusion_reason' not in data or data.get('exclusion_reason') is None) and metrics.get('stage2_exclusion_reason'):
            data['exclusion_reason'] = metrics.get('stage2_exclusion_reason')
        for key in ('recommendation_tier', 'recommendation_book', 'execution_lane', 'touch_window_band'):
            if data.get(key) is None:
                data.pop(key, None)
        if data.get('monitor_cadence_minutes') is None:
            data.pop('monitor_cadence_minutes', None)
        return data

    symbol: str
    company_name: Optional[str] = None
    mover_rank: int = 0
    intraday_pct_gain: float = 0.0
    advanced_to_stage2: bool = False
    exclusion_reason: Optional[str] = None
    current_price: Optional[float] = None
    current_cum_volume: Optional[float] = None
    relative_volume: Optional[float] = None
    total_score: Optional[float] = None
    recommendation_tier: str = 'rejected'
    recommendation_book: str = 'rejected'
    structural_score: Optional[float] = None
    entry_touch_likelihood_score: Optional[float] = None
    touch_urgency_score: Optional[float] = None
    expected_minutes_to_touch: Optional[float] = None
    follow_through_confidence_score: Optional[float] = None
    expected_actionability_score: Optional[float] = None
    actionability_score: Optional[float] = None
    headline_rank_score: Optional[float] = None
    pullback_queue_rank_score: Optional[float] = None
    queue_actionability_score: Optional[float] = None
    queue_escalation_score: Optional[float] = None
    execution_lane: str = 'passive_watchlist'
    monitor_cadence_minutes: int = 0
    queue_priority_score: Optional[float] = None
    touch_window_band: str = 'unlikely_in_window'
    component_scores: Dict[str, float] = Field(default_factory=dict)
    metrics: Dict[str, Any] = Field(default_factory=dict)
    rationale: str = ''
    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    target_price: Optional[float] = None
    stretch_target_price: Optional[float] = None
    stop_price: Optional[float] = None
    chart_context: ChartContext = Field(default_factory=ChartContext)


# ---------------------------------------------------------------------------
# Validation contracts
# ---------------------------------------------------------------------------

class EntryMethodology(BaseModel):
    requires_post_scan_touch: bool = False
    disallows_same_bar_target_credit: bool = False
    description: str = ''


class ReplayHonesty(BaseModel):
    entry_fill_mode: str = 'legacy'
    target_hit_mode: str = 'legacy'


class BaselineComparison(BaseModel):
    class MoverRankOnly(BaseModel):
        precision_at_5: float = 0.0
        precision_at_10: float = 0.0
        precision_at_20: float = 0.0
        overall_hit_rate: float = 0.0
        eligible_rows: int = 0

    class DeltaVsMoverRank(BaseModel):
        precision_at_5: float = 0.0
        precision_at_10: float = 0.0
        precision_at_20: float = 0.0

    mover_rank_only: MoverRankOnly = Field(default_factory=MoverRankOnly)
    stage2_delta_vs_mover_rank: DeltaVsMoverRank = Field(default_factory=DeltaVsMoverRank)


class ScoreBucketRow(BaseModel):
    bucket: str
    count: int = 0
    hit_rate: float = 0.0


class ScoreBucketMonotonicity(BaseModel):
    ok: bool = False
    ratio: float = 0.0
    pairs_tested: int = 0
    pairs_non_decreasing: int = 0


class ValidationVerdictCheck(BaseModel):
    name: str
    passed: Optional[bool] = None
    actual: Optional[Any] = None


class ValidationVerdict(BaseModel):
    verdict: str = 'UNKNOWN'
    checks: List[ValidationVerdictCheck] = Field(default_factory=list)


class StageFunnelRates(BaseModel):
    model_config = ConfigDict(extra='allow')

    advanced_from_scored: float = 0.0
    headline_from_advanced: float = 0.0
    ready_now_from_advanced: float = 0.0
    near_ready_from_advanced: float = 0.0
    entry_accessible_from_advanced: float = 0.0
    entry_touched_from_advanced: float = 0.0
    follow_through_from_entry_touched: float = 0.0


class StageFunnelSummary(BaseModel):
    model_config = ConfigDict(extra='allow')

    scored_rows: int = 0
    advanced_stage2: int = 0
    headline_shortlist: int = 0
    ready_now: int = 0
    near_ready: int = 0
    watchlist: int = 0
    entry_accessible: int = 0
    entry_touched: int = 0
    follow_through_positive: int = 0
    rates: StageFunnelRates = Field(default_factory=StageFunnelRates)


class DownloadSummary(BaseModel):
    cache_enabled: bool = False
    cache_hits: int = 0
    cache_misses: int = 0
    downloaded_segments: int = 0
    days_requested: Optional[int] = None


class RangeStructureDistribution(BaseModel):
    model_config = ConfigDict(extra='allow')

    A: int = 0
    B: int = 0
    C: int = 0


class StageLabelSummary(BaseModel):
    structural_tradability_positive_count: int = 0
    entry_accessibility_positive_count: int = 0
    follow_through_quality_positive_count: int = 0
    structural_tradability_positive_rate_scored: float = 0.0
    entry_accessibility_positive_rate_stage2: float = 0.0
    follow_through_quality_positive_rate_entry_touched: float = 0.0


class ValidationSummary(BaseModel):
    model_config = ConfigDict(extra='allow')
    """
    Summary payload stored in validation_runs.summary_json.

    This is the canonical schema. Any new fields must be added here first.
    The _ensure_validation_summary_compat() shim in main.py should shrink
    over time as all runs are generated against this contract.
    """
    days: int = 0
    top50_stage1_per_day: int = 50
    scored_replay_rows_total: int = 0
    advanced_stage2_total: int = 0
    overall_hit_rate: float = 0.0
    precision_at_5: float = 0.0
    precision_at_10: float = 0.0
    precision_at_20: float = 0.0
    conditional_hit_rate_entry_touched: float = 0.0
    conditional_precision_at_5_entry_touched: float = 0.0
    conditional_precision_at_10_entry_touched: float = 0.0
    conditional_precision_at_20_entry_touched: float = 0.0
    avg_mfe_pct: Optional[float] = None
    avg_mae_pct: Optional[float] = None
    median_minutes_to_entry: Optional[int] = None
    median_minutes_to_target: Optional[int] = None
    entry_touch_rate_stage2: float = 0.0
    entry_touch_rate_scored: float = 0.0
    avg_completed_cycles_stage2: float = 0.0
    avg_lower_zone_touches_stage2: float = 0.0
    avg_upper_zone_touches_stage2: float = 0.0

    stage_label_summary: StageLabelSummary = Field(default_factory=StageLabelSummary)
    tier_summary: List[Dict[str, Any]] = Field(default_factory=list)
    book_summary: List[Dict[str, Any]] = Field(default_factory=list)
    touch_window_summary: List[Dict[str, Any]] = Field(default_factory=list)
    stage_funnel_summary: StageFunnelSummary = Field(default_factory=StageFunnelSummary)
    entry_methodology: EntryMethodology = Field(default_factory=EntryMethodology)
    replay_honesty: ReplayHonesty = Field(default_factory=ReplayHonesty)
    score_bucket_summary: List[ScoreBucketRow] = Field(default_factory=list)
    mover_bucket_summary: List[Dict[str, Any]] = Field(default_factory=list)
    false_positives_sample: List[Dict[str, Any]] = Field(default_factory=list)
    daily_summaries: List[Dict[str, Any]] = Field(default_factory=list)
    score_bucket_monotonicity: ScoreBucketMonotonicity = Field(default_factory=ScoreBucketMonotonicity)
    baseline_comparison: BaselineComparison = Field(default_factory=BaselineComparison)
    range_structure_distribution: RangeStructureDistribution = Field(default_factory=RangeStructureDistribution)
    validation_verdict: ValidationVerdict = Field(default_factory=ValidationVerdict)
    download_summary: DownloadSummary = Field(default_factory=DownloadSummary)

    # Optional calibration result (attached by research runner)
    calibration: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Research contracts
# ---------------------------------------------------------------------------

class OffsetResultRow(BaseModel):
    model_config = ConfigDict(extra='allow')

    scan_offset_minutes: int
    validation_id: int
    advanced_rows: int = 0
    precision_at_10: float = 0.0
    conditional_precision_at_10: float = 0.0
    overall_hit_rate: float = 0.0
    entry_touch_rate: float = 0.0
    actionable_lane: int = 0
    utility_score: float = 0.0
    passed_gates: bool = False


class SchedulePlan(BaseModel):
    default_scan_offset_minutes: int
    suggested_scheduled_scan_offsets: str = ''
    recommendation_basis: str = ''
    all_offsets_failed_gates: bool = False
    primary_validation_id: int = 0
    secondary_validation_id: Optional[int] = None


class ResearchResult(BaseModel):
    """Result payload stored in research_runs.result_json."""
    model_config = ConfigDict(extra='allow')

    validation_id: Optional[int] = None
    best_validation_id: Optional[int] = None
    summary: Optional[Dict[str, Any]] = None
    calibration: Optional[Dict[str, Any]] = None
    auto_applied: bool = False
    auto_applied_schedule: bool = False

    # Canonical runtime fields
    offset_ladder_summary: Optional[List[Dict[str, Any]]] = None
    recommended_live_schedule: Optional[Dict[str, Any]] = None
    validation_ids_by_offset: Optional[Dict[str, Any]] = None
    traceback_tail: Optional[List[str]] = None
    mode: Optional[str] = None

    # Backward-compat aliases used by older code/tests
    offset_rows: Optional[List[Dict[str, Any]]] = None
    schedule_plan: Optional[Dict[str, Any]] = None

    @model_validator(mode='before')
    @classmethod
    def _coerce_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if not data.get('offset_ladder_summary') and data.get('offset_rows') is not None:
            data['offset_ladder_summary'] = data.get('offset_rows')
        if not data.get('offset_rows') and data.get('offset_ladder_summary') is not None:
            data['offset_rows'] = data.get('offset_ladder_summary')
        if not data.get('recommended_live_schedule') and data.get('schedule_plan') is not None:
            data['recommended_live_schedule'] = data.get('schedule_plan')
        if not data.get('schedule_plan') and data.get('recommended_live_schedule') is not None:
            data['schedule_plan'] = data.get('recommended_live_schedule')
        return data


# ---------------------------------------------------------------------------
# Normalization helpers used at runtime boundaries
# ---------------------------------------------------------------------------

def normalize_scan_summary(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    return ScanSummary(**(payload or {})).model_dump(exclude_none=True)


def normalize_candidate_payload(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    return CandidatePayload(**(payload or {})).model_dump(exclude_none=True)


def normalize_validation_summary(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    return ValidationSummary(**(payload or {})).model_dump(exclude_none=True)


def normalize_research_result(payload: Dict[str, Any] | None) -> Dict[str, Any]:
    return ResearchResult(**(payload or {})).model_dump(exclude_none=True)
