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

    operating_posture = (
        f"Production, {settings.trading_mode}, live trading {'enabled' if settings.enable_live_trading else 'disabled'}, "
        f"{settings.alpaca_data_feed.upper()} data, scheduled checkpoints {', '.join(str(x) for x in settings.scheduled_offsets)} minutes."
    )

    if best_shadow_profile in {'soft_bounce_quality', 'combined_soft_structure'}:
        pressure_point = (
            'Strict structural classifier settings still look like the most likely future pressure point, '
            f'especially {best_shadow_profile.replace("_", " ")}. No live change is justified yet.'
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

    if best_shadow_profile:
        what_matters_now.append(
            f'Best current shadow profile is {best_shadow_profile}; treat it as a candidate for later trial only after the promotion gate opens.'
        )
    if clean_day_count < 5:
        what_matters_now.append(
            f'Evidence density is still thin ({clean_day_count} clean day{'s' if clean_day_count != 1 else ''}); accumulation matters more than tuning right now.'
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
        'The single decision bundle or post-close refresh path shows a reliability break that blocks evidence collection.',
        'The automated evidence begins pointing at a different bottleneck than the current strict structural classifier story.',
    ]

    if readiness == 'eligible_for_narrow_live_trial':
        overall_assessment = (
            'The app is in a promotion-ready state for a narrow controlled trial, but live thresholds should still change only via an explicit approval step.'
        )
    elif readiness == 'shadow_profile_promising_but_early':
        overall_assessment = (
            'The app looks coherent for the current evidence-first phase. The most likely future change area is structural-classifier softness, '
            'but the current evidence is still too thin for a live calibration change.'
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
    }


def build_goal_alignment_text(summary: dict[str, Any]) -> str:
    lines = [
        'Goal alignment readout',
        f"Generated at UTC: {summary.get('generated_at_utc')}",
        '',
        'Objective',
        summary.get('objective_summary') or '',
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
    lines.extend(f'- {item}' for item in summary.get('what_matters_now') or [])
    lines.extend(['', 'What is frozen'])
    lines.extend(f'- {item}' for item in summary.get('what_is_frozen') or [])
    lines.extend(['', 'What would justify change'])
    lines.extend(f'- {item}' for item in summary.get('what_would_justify_change') or [])
    lines.extend(['', 'Machine state', json.dumps({
        'latest_selected_day': summary.get('latest_selected_day'),
        'clean_day_count': summary.get('clean_day_count'),
        'best_shadow_profile': summary.get('best_shadow_profile'),
        'overall_promotion_readiness': summary.get('overall_promotion_readiness'),
        'decision_recommendation_code': summary.get('decision_recommendation_code'),
        'tradable_universe_count': summary.get('tradable_universe_count'),
    }, indent=2)])
    return '\n'.join(lines).strip() + '\n'
