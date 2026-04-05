from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import Settings

UTC = timezone.utc

OBJECTIVE_SUMMARY = (
    'Surface a small, trustworthy set of Russell 2000 names that are genuinely suitable '
    'for repeatable intraday +1% range trades before the cutoff.'
)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, '__dict__'):
        return {k: v for k, v in vars(value).items() if not k.startswith('_')}
    return {}


def build_goal_alignment_summary(
    settings: Settings,
    *,
    universe_status: Any,
    decision_state: dict[str, Any] | None,
) -> dict[str, Any]:
    universe = _as_dict(universe_status)
    decision = dict(decision_state or {})

    clean_day_count = int(decision.get('clean_day_count') or 0)
    best_shadow_profile = str(decision.get('best_shadow_profile') or '') or None
    readiness = str(decision.get('overall_promotion_readiness') or 'insufficient_runtime_context')
    recommendation = str(decision.get('decision_recommendation_code') or '')
    currently_valid_now_count = int(decision.get('currently_valid_now_count') or 0)
    regressed_count = int(decision.get('regressed_after_earlier_validity_count') or 0)
    replay_summary = dict(decision.get('historical_replay_shadow') or {})
    replay_bottleneck = dict(decision.get('historical_replay_bottleneck') or {})
    replay_compatibility = dict(decision.get('historical_replay_checkpoint_compatibility') or {})
    replay_available = bool(replay_summary.get('available'))
    replay_profile = ((replay_summary.get('recommended_profile') or {}) or {}).get('profile_name')
    replay_verdict = str(replay_summary.get('overall_verdict') or '')
    replay_trading_days = int(replay_summary.get('trading_day_count') or 0)
    best_replay_offset = dict(replay_bottleneck.get('best_offset_by_tradeable_share') or {})
    worst_replay_offset = dict(replay_bottleneck.get('worst_offset_by_tradeable_share') or {})
    best_replay_offset_minutes = int(best_replay_offset.get('scan_offset_minutes') or 0)
    worst_replay_offset_minutes = int(worst_replay_offset.get('scan_offset_minutes') or 0)
    best_replay_offset_share = best_replay_offset.get('tradeable_share')
    worst_replay_offset_share = worst_replay_offset.get('tradeable_share')
    replay_gate = dict(replay_compatibility.get('best_gate_weaker_offset_only') or {}) or dict(replay_compatibility.get('best_gate_weaker_offset_total') or {})
    replay_gate_metric = replay_gate.get('metric_name')
    replay_gate_comparator = replay_gate.get('comparator')
    replay_gate_threshold = replay_gate.get('threshold_value')
    replay_gate_share = replay_gate.get('tradeable_share')
    checkpoint_specific_replay_support = recommendation == 'historical_replay_supports_checkpoint_specific_candidate_hold_live_gate'
    checkpoint_gate_shadow_test = recommendation == 'historical_replay_supports_weaker_checkpoint_gate_shadow_test_hold_live_gate'

    shadow_visual_review = dict(decision.get('shadow_visual_review') or {})
    visual_summary = dict(shadow_visual_review.get('summary') or {})
    visual_verdict_counts = dict(visual_summary.get('visual_review_verdict_counts') or {})
    visual_selected_count = int(shadow_visual_review.get('selected_review_count') or visual_summary.get('selected_review_count') or 0)
    visual_all_trend_biased = bool(shadow_visual_review.get('all_tradeable_but_trend_biased'))

    replay_supported_visual_review = dict(decision.get('replay_supported_visual_review') or {})
    replay_visual_summary = dict(replay_supported_visual_review.get('summary') or {})
    replay_visual_verdict_counts = dict(replay_visual_summary.get('visual_review_verdict_counts') or {})
    replay_visual_selected_count = int(replay_supported_visual_review.get('selected_review_count') or replay_visual_summary.get('selected_review_count') or 0)
    replay_visual_supports_range = bool(replay_supported_visual_review.get('supports_range_cycling_thesis'))
    replay_visual_all_trend_biased = bool(replay_supported_visual_review.get('all_tradeable_but_trend_biased'))
    replay_visual_focus_profile = str(replay_supported_visual_review.get('focus_profile_name') or replay_visual_summary.get('focus_profile_name') or '') or None
    replay_visual_focus_offset = int(replay_supported_visual_review.get('focus_offset_minutes') or replay_visual_summary.get('focus_offset_minutes') or 0)

    surfaced_visual = dict(decision.get('surfaced_checkpoint_visual_review') or {})
    surfaced_visual_summary = dict(surfaced_visual.get('summary') or {})
    surfaced_visual_verdict_counts = dict(surfaced_visual_summary.get('visual_review_verdict_counts') or {})
    surfaced_visual_selected_count = int(surfaced_visual.get('selected_review_count') or surfaced_visual_summary.get('selected_review_count') or 0)
    surfaced_visual_supports_range = bool(surfaced_visual.get('supports_range_cycling_thesis'))
    surfaced_visual_focus_offset = int(surfaced_visual.get('focus_offset_minutes') or surfaced_visual_summary.get('focus_offset_minutes') or 0)

    surfaced_multisession_visual = dict(decision.get('surfaced_multisession_visual_review') or {})
    surfaced_multisession_summary = dict(surfaced_multisession_visual.get('summary') or {})
    surfaced_multisession_verdict_counts = dict(surfaced_multisession_summary.get('visual_review_verdict_counts') or {})
    surfaced_multisession_selected_count = int(surfaced_multisession_visual.get('selected_review_count') or surfaced_multisession_summary.get('selected_review_count') or 0)
    surfaced_multisession_supports_range = bool(surfaced_multisession_visual.get('supports_range_cycling_thesis'))
    surfaced_multisession_all_non_supportive = bool(surfaced_multisession_visual.get('all_rows_non_supportive'))
    surfaced_multisession_focus_offset = int(surfaced_multisession_visual.get('focus_offset_minutes') or surfaced_multisession_summary.get('focus_offset_minutes') or 0)

    operating_posture = (
        f"Production, {settings.trading_mode}, live trading {'enabled' if settings.enable_live_trading else 'disabled'}, "
        f"{settings.alpaca_data_feed.upper()} data, scheduled checkpoints {', '.join(str(x) for x in settings.scheduled_offsets)} minutes."
    )

    if surfaced_multisession_all_non_supportive and surfaced_multisession_selected_count > 0:
        pressure_point = (
            'Actual surfaced stage-2 names across recent sessions are still failing thesis validation: '
            f'all {surfaced_multisession_selected_count} reviewed surfaced rows at the best checkpoint were either trend-biased or not actionable from the preferred entry zone. '
            'Do not treat the current surfaced path as product-valid yet.'
        )
    elif replay_visual_all_trend_biased and replay_visual_selected_count > 0:
        pressure_point = (
            'The surviving replay-supported path now looks thesis-misaligned in recent live-shaped charts: '
            f'all {replay_visual_selected_count} reviewed {replay_visual_focus_profile or "replay-supported"} rows at {replay_visual_focus_offset} minutes were trend-biased rather than clean range-cycling setups. '
            'Do not treat replay support alone as product-valid yet.'
        )
    elif replay_visual_supports_range and replay_visual_selected_count > 0:
        pressure_point = (
            'The clearest near-term pressure point is now live-shaped confirmation of the replay-supported branch: '
            f'{replay_visual_focus_profile or replay_profile or "the replay-supported profile"} at {replay_visual_focus_offset} minutes now has automated visual support, '
            'but live behavior should still remain frozen until narrower confirmation is earned.'
        )
    elif visual_all_trend_biased and visual_selected_count > 0:
        pressure_point = (
            'Thesis fidelity now looks like the clearest near-term pressure point: '
            f'automated visual review marked all {visual_selected_count} reviewed rescues as trend-biased rather than clean range-cycling setups. '
            'Do not treat raw tradeability alone as support for classifier softening.'
        )
    elif (checkpoint_specific_replay_support or checkpoint_gate_shadow_test) and replay_profile and best_replay_offset_minutes > 0 and worst_replay_offset_minutes > 0:
        pressure_point = (
            'Checkpoint-specific decay now looks like the clearest near-term pressure point: '
            f'{replay_profile.replace("_", " ")} is replay-supported at {best_replay_offset_minutes} minutes '
            f'but materially weaker by {worst_replay_offset_minutes} minutes. No live change is justified yet.'
        )
    elif replay_profile in {'soft_bounce_quality', 'combined_soft_structure'} or best_shadow_profile in {'soft_bounce_quality', 'combined_soft_structure'}:
        focus_profile = replay_profile or best_shadow_profile
        pressure_point = (
            'Strict structural classifier settings still look like the most likely future pressure point, '
            f'especially {focus_profile.replace("_", " ")}. No live change is justified yet.'
        )
    else:
        pressure_point = (
            'Strict structural classifier settings remain the likely future pressure point. '
            'Universe and liquidity settings do not currently look like the main bottleneck.'
        )

    what_matters_now = [
        f'Keep the app stable in {settings.trading_mode} mode and let clean 120/150-minute evidence accumulate.',
        f'Use the decision bundle as the single source of truth; current recommendation is {recommendation or "not available yet"}.',
        f'Watch clean-day count, currently-valid-now count ({currently_valid_now_count}), and regressed-later count ({regressed_count}) instead of adding more scanner machinery.',
        f'Keep universe and liquidity posture stable while the evidence gate matures; tradable universe currently reports {universe.get("tradable_count", "unknown")} names.',
    ]

    if replay_available:
        suffix = 's' if replay_trading_days != 1 else ''
        what_matters_now.append(
            f'Use historical replay under the current clean logic as the primary evidence engine; cached replay currently covers {replay_trading_days} trading day{suffix} and points most strongly at {replay_profile or "no clear profile yet"}.'
        )
        if best_replay_offset_minutes > 0:
            what_matters_now.append(
                f'Replay currently supports {replay_profile or "the leading profile"} most strongly at the {best_replay_offset_minutes}-minute checkpoint ({best_replay_offset_share}), while the weaker checkpoint remains below support ({worst_replay_offset_minutes}m at {worst_replay_offset_share}).'
            )
        if surfaced_multisession_supports_range and surfaced_multisession_selected_count > 0:
            what_matters_now.append(
                'Actual surfaced stage-2 names now include visually supportive range-cycling examples at the best checkpoint. Treat that as stronger product-truth on the surfaced path while keeping live behavior frozen.'
            )
        if surfaced_multisession_all_non_supportive and surfaced_multisession_selected_count > 0:
            what_matters_now.append(
                'Actual surfaced stage-2 names across recent sessions are still either trend-biased or not actionable from the preferred entry zone. Any next change should narrow upstream selection rather than loosen live behavior.'
            )
        if replay_visual_supports_range and replay_visual_selected_count > 0:
            what_matters_now.append(
                f'Automated visual review supports the replay-backed {replay_visual_focus_profile or replay_profile or "profile"} path at {replay_visual_focus_offset} minutes on recent live-shaped rows. Treat that as advisory only while live behavior stays frozen.'
            )
        if replay_visual_all_trend_biased and replay_visual_selected_count > 0:
            what_matters_now.append(
                f'Automated visual review flags the replay-backed {replay_visual_focus_profile or replay_profile or "profile"} path at {replay_visual_focus_offset} minutes as trend-biased on all reviewed rows. Do not treat replay support for that branch as product-valid yet.'
            )
        if checkpoint_gate_shadow_test and replay_gate_metric:
            what_matters_now.append(
                f'Compatibility analysis now supports a weaker-checkpoint shadow gate candidate using {replay_gate_metric} {replay_gate_comparator} {replay_gate_threshold} ({replay_gate_share}). Treat that as a shadow-test input only, not as permission for a live threshold change.'
            )
        what_matters_now.append(
            'Treat live clean days as the release gate on top of replay evidence, not as the main statistical source of confidence.'
        )
    else:
        what_matters_now.append(
            'Historical replay under the current clean logic should be treated as the primary evidence engine as soon as the replay bundle is generated.'
        )

    if best_shadow_profile:
        what_matters_now.append(
            f'Best current shadow profile is {best_shadow_profile}; treat it as a candidate for later trial only after the promotion gate opens.'
        )
    if clean_day_count < 5:
        suffix = 's' if clean_day_count != 1 else ''
        what_matters_now.append(
            f'Evidence density is still thin ({clean_day_count} clean day{suffix}); accumulation matters more than tuning right now.'
        )
    if visual_all_trend_biased and visual_selected_count > 0:
        what_matters_now.append(
            f'Automated visual review currently shows all {visual_selected_count} reviewed rescues were tradeable but trend-biased, not clean range-cycling setups. Keep live thresholds frozen and do not count that as thesis-valid support for classifier softening.'
        )
    if surfaced_visual_selected_count > 0 and not surfaced_visual_supports_range:
        what_matters_now.append(
            f'Actual surfaced names at the latest best checkpoint ({surfaced_visual_focus_offset}m) were not clearly thesis-valid on the latest reviewed day. Use the multi-session surfaced review to decide whether that was noise or the real product truth.'
        )

    frozen_now = [
        'Live threshold changes remain frozen until the automated promotion gate opens.',
        'Stage-1 redesign stays frozen; shortlist alignment is not the main uncertainty right now.',
        'New scoring features and queue/tier expansion stay frozen because they would add complexity without better evidence.',
        'Presentation-first UI work stays frozen unless it materially reduces operational steps or prevents drift.',
    ]

    justify_change = [
        'A shadow profile reaches eligible_for_narrow_live_trial under the app’s own promotion rule.',
        'Multiple additional clean sessions keep showing the same overstrict pattern while false positives remain controlled.',
        'Historical replay under the current clean logic keeps supporting the same profile across a larger sample instead of only a single clean live day.',
        'The single decision bundle or post-close refresh path shows a reliability break that blocks evidence collection.',
        'The automated evidence begins pointing at a different bottleneck than the current strict structural classifier story.',
    ]

    if surfaced_multisession_all_non_supportive and surfaced_multisession_selected_count > 0:
        overall_assessment = (
            'The app is still not successful yet because the actual surfaced stage-2 names across recent sessions are not yielding clean range-cycling setups at the best checkpoint. Keep live behavior frozen and stop treating rescue-path evidence as the main question.'
        )
    elif replay_visual_all_trend_biased and replay_visual_selected_count > 0:
        overall_assessment = (
            'The app is still not successful yet because the only surviving replay-supported branch now looks trend-biased rather than thesis-valid on recent live-shaped charts. Keep live behavior frozen and do not treat replay support by itself as product success.'
        )
    elif replay_visual_supports_range and replay_visual_selected_count > 0:
        overall_assessment = (
            'The app is closer to the true objective because the replay-supported path now also has automated visual support on recent live-shaped rows. Live behavior should still stay frozen; the next move should validate that exact supported path more narrowly, not broaden scoring or threshold changes.'
        )
    elif visual_all_trend_biased and visual_selected_count > 0:
        overall_assessment = (
            'The app is still not successful yet because the currently reviewed would-be rescues look tradeable only in a directional, trend-biased way rather than as clean range-cycling setups. The next move should tighten thesis fidelity in the machine state, not loosen live behavior.'
        )
    elif readiness == 'eligible_for_narrow_live_trial':
        overall_assessment = (
            'The app is in a promotion-ready state for a narrow controlled trial, but live thresholds should still change only via an explicit approval step.'
        )
    elif replay_verdict == 'historical_replay_supports_candidate_profile':
        overall_assessment = (
            'The app now has a replay-backed candidate profile under the current clean logic. Live behavior should still stay frozen until the release gate opens on clean live evidence.'
        )
    elif readiness == 'shadow_profile_promising_but_early':
        overall_assessment = (
            'The app looks coherent for the current evidence-first phase. The most likely future change area is structural-classifier softness, but the current evidence is still too thin for a live calibration change.'
        )
    elif readiness == 'insufficient_runtime_context':
        overall_assessment = (
            'The app is not yet in a state where promotion decisions should be trusted. Resolve runtime or evidence availability first.'
        )
    else:
        overall_assessment = (
            'The app is operationally coherent, but the evidence gate has not opened. Hold live behavior steady and keep accumulating clean sessions.'
        )

    return {
        'generated_at_utc': datetime.now(UTC).isoformat(),
        'objective_summary': OBJECTIVE_SUMMARY,
        'operating_posture': operating_posture,
        'overall_assessment': overall_assessment,
        'likely_future_pressure_point': pressure_point,
        'what_matters_now': what_matters_now,
        'what_is_frozen': frozen_now,
        'what_would_justify_change': justify_change,
        'clean_day_count': clean_day_count,
        'best_shadow_profile': best_shadow_profile,
        'overall_promotion_readiness': readiness,
        'decision_recommendation_code': recommendation,
        'latest_selected_day': decision.get('latest_selected_day'),
        'tradable_universe_count': universe.get('tradable_count'),
        'historical_replay_available': replay_available,
        'historical_replay_best_profile': replay_profile,
        'historical_replay_overall_verdict': replay_verdict,
        'historical_replay_trading_day_count': replay_trading_days,
        'historical_replay_best_supported_offset_minutes': best_replay_offset_minutes or None,
        'historical_replay_best_supported_offset_share': best_replay_offset_share,
        'historical_replay_worst_offset_minutes': worst_replay_offset_minutes or None,
        'historical_replay_worst_offset_share': worst_replay_offset_share,
        'historical_replay_weaker_checkpoint_gate_metric': replay_gate_metric,
        'historical_replay_weaker_checkpoint_gate_comparator': replay_gate_comparator,
        'historical_replay_weaker_checkpoint_gate_threshold': replay_gate_threshold,
        'historical_replay_weaker_checkpoint_gate_share': replay_gate_share,
        'shadow_visual_review_selected_count': visual_selected_count,
        'shadow_visual_review_verdict_counts': visual_verdict_counts,
        'shadow_visual_review_all_tradeable_but_trend_biased': visual_all_trend_biased,
        'replay_supported_visual_review_selected_count': replay_visual_selected_count,
        'replay_supported_visual_review_verdict_counts': replay_visual_verdict_counts,
        'replay_supported_visual_review_supports_range_cycling_thesis': replay_visual_supports_range,
        'replay_supported_visual_review_all_tradeable_but_trend_biased': replay_visual_all_trend_biased,
        'surfaced_checkpoint_visual_review_selected_count': surfaced_visual_selected_count,
        'surfaced_checkpoint_visual_review_verdict_counts': surfaced_visual_verdict_counts,
        'surfaced_checkpoint_visual_review_supports_range_cycling_thesis': surfaced_visual_supports_range,
        'surfaced_multisession_visual_review_selected_count': surfaced_multisession_selected_count,
        'surfaced_multisession_visual_review_verdict_counts': surfaced_multisession_verdict_counts,
        'surfaced_multisession_visual_review_supports_range_cycling_thesis': surfaced_multisession_supports_range,
        'surfaced_multisession_visual_review_all_rows_non_supportive': surfaced_multisession_all_non_supportive,
    }


def build_goal_alignment_text(summary: dict[str, Any]) -> str:
    sections = [
        'Goal alignment readout',
        '',
        f"Generated at: {summary.get('generated_at_utc')}",
        f"Objective: {summary.get('objective_summary')}",
        '',
        'Operating posture',
        summary.get('operating_posture') or '',
        '',
        'Overall assessment',
        summary.get('overall_assessment') or '',
        '',
        'Likely future pressure point',
        summary.get('likely_future_pressure_point') or '',
        '',
        'What matters now',
    ]
    for item in summary.get('what_matters_now') or []:
        sections.append(f'- {item}')
    sections.extend(['', 'What is frozen'])
    for item in summary.get('what_is_frozen') or []:
        sections.append(f'- {item}')
    sections.extend(['', 'What would justify change'])
    for item in summary.get('what_would_justify_change') or []:
        sections.append(f'- {item}')
    sections.extend(['', 'Machine state snapshot'])
    for key in [
        'latest_selected_day', 'clean_day_count', 'best_shadow_profile', 'overall_promotion_readiness',
        'decision_recommendation_code', 'tradable_universe_count', 'historical_replay_available',
        'historical_replay_best_profile', 'historical_replay_overall_verdict', 'historical_replay_trading_day_count',
        'historical_replay_best_supported_offset_minutes', 'historical_replay_best_supported_offset_share',
        'historical_replay_worst_offset_minutes', 'historical_replay_worst_offset_share',
        'historical_replay_weaker_checkpoint_gate_metric', 'historical_replay_weaker_checkpoint_gate_comparator',
        'historical_replay_weaker_checkpoint_gate_threshold', 'historical_replay_weaker_checkpoint_gate_share',
        'shadow_visual_review_all_tradeable_but_trend_biased'
    ]:
        if key in summary:
            sections.append(f'- {key}: {summary.get(key)}')
    return '\n'.join(sections) + '\n'


def write_goal_alignment_snapshot(path: str, summary: dict[str, Any]) -> None:
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(build_goal_alignment_text(summary))
        fh.write('\n')
        fh.write(json.dumps(summary, indent=2))
