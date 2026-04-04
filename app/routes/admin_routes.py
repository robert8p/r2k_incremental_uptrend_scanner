from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.config import save_settings_override


def _render_text_snapshot(title: str, payload: object) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    return f"{title}\nGenerated at UTC: {generated_at}\n\n{body}\n"


def _text_attachment(filename: str, title: str, payload: object) -> Response:
    return Response(
        content=_render_text_snapshot(title, payload),
        media_type='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


def register_admin_routes(
    app: FastAPI,
    *,
    runtime,
    safe_render: Callable,
) -> None:
    @app.get('/settings', response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return safe_render(request, 'settings.html', {'config': runtime.settings.public_snapshot()})

    @app.post('/settings/save')
    def settings_save(
        trading_mode: str = Form(...),
        default_notional_usd: float = Form(...),
        min_price: float = Form(...),
        min_avg_dollar_volume: float = Form(...),
        min_intraday_dollar_volume: float = Form(...),
        low_price_hard_floor: float = Form(...),
        target_pct: float = Form(...),
        stretch_target_pct: float = Form(...),
        default_scan_offset_minutes: int = Form(...),
        scheduled_scan_offsets: str = Form(...),
        weight_target_strength: float = Form(...),
        weight_liquidity: float = Form(...),
        weight_volatility: float = Form(...),
        weight_dynamic_range: float = Form(...),
        weight_range_position: float = Form(...),
        weight_time_feasibility: float = Form(...),
        weight_execution_quality: float = Form(...),
    ) -> RedirectResponse:
        save_settings_override(
            runtime.settings,
            {
                'trading_mode': trading_mode,
                'default_notional_usd': default_notional_usd,
                'min_price': min_price,
                'min_avg_dollar_volume': min_avg_dollar_volume,
                'min_intraday_dollar_volume': min_intraday_dollar_volume,
                'low_price_hard_floor': low_price_hard_floor,
                'target_pct': target_pct,
                'stretch_target_pct': stretch_target_pct,
                'default_scan_offset_minutes': default_scan_offset_minutes,
                'scheduled_scan_offsets': scheduled_scan_offsets,
                'weight_target_strength': weight_target_strength,
                'weight_liquidity': weight_liquidity,
                'weight_volatility': weight_volatility,
                'weight_dynamic_range': weight_dynamic_range,
                'weight_range_position': weight_range_position,
                'weight_time_feasibility': weight_time_feasibility,
                'weight_execution_quality': weight_execution_quality,
            },
        )
        runtime.refresh()
        return RedirectResponse(url='/settings', status_code=303)

    @app.get('/diagnostics', response_class=HTMLResponse)
    def diagnostics_page(request: Request) -> HTMLResponse:
        from app.services.diagnostics import build_diagnostics_snapshot, read_recent_logs
        snapshot = build_diagnostics_snapshot(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            scheduler_status=runtime.scheduler_status,
        )
        logs = read_recent_logs(runtime.settings)
        return safe_render(request, 'diagnostics.html', {'snapshot': snapshot, 'logs': logs})


    @app.get('/diagnostics/config-snapshot.txt')
    def diagnostics_config_snapshot() -> Response:
        return _text_attachment(
            'config_snapshot.txt',
            'Config snapshot',
            runtime.settings.public_snapshot(),
        )

    @app.get('/diagnostics/universe-snapshot.txt')
    def diagnostics_universe_snapshot() -> Response:
        from app.services.diagnostics import build_diagnostics_snapshot

        snapshot = build_diagnostics_snapshot(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            scheduler_status=runtime.scheduler_status,
        )
        return _text_attachment(
            'universe_snapshot.txt',
            'Universe snapshot',
            snapshot.get('universe_status', {}),
        )

    @app.get('/diagnostics/goal-alignment.txt')
    def diagnostics_goal_alignment_snapshot() -> Response:
        from app.services.diagnostics import build_diagnostics_snapshot
        from app.services.goal_alignment import build_goal_alignment_text

        snapshot = build_diagnostics_snapshot(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            scheduler_status=runtime.scheduler_status,
        )
        summary = snapshot.get('goal_alignment', {})
        content = build_goal_alignment_text(summary)
        return Response(
            content=content,
            media_type='text/plain; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename=goal_alignment.txt'},
        )


    @app.get('/diagnostics/live-confirmation-pack.zip')
    def diagnostics_live_confirmation_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.live_confirmation_pack import build_live_confirmation_pack
        content = pack_to_zip_bytes(
            build_live_confirmation_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=5,
                offsets=[120, 150],
                scheduler_status=runtime.scheduler_status,
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=live_confirmation_pack_last_5_sessions.zip'},
        )

    @app.get('/diagnostics/stage2-sanity-pack.zip')
    def diagnostics_stage2_sanity_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.stage2_sanity_pack import build_stage2_sanity_pack

        content = pack_to_zip_bytes(
            build_stage2_sanity_pack(
                runtime.settings,
                runtime.db,
                days=5,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=stage2_sanity_pack_last_5_sessions.zip'},
        )

    @app.get('/diagnostics/stage2-regression-pack.zip')
    def diagnostics_stage2_regression_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.stage2_regression_pack import build_stage2_regression_pack

        content = pack_to_zip_bytes(
            build_stage2_regression_pack(
                runtime.settings,
                runtime.db,
                days=5,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=stage2_regression_pack_last_5_sessions.zip'},
        )

    @app.get('/diagnostics/checkpoint-decision-pack.zip')
    def diagnostics_checkpoint_decision_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.checkpoint_decision_surface import build_checkpoint_decision_pack

        content = pack_to_zip_bytes(
            build_checkpoint_decision_pack(
                runtime.settings,
                runtime.db,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=checkpoint_decision_pack_latest_day.zip'},
        )

    @app.get('/diagnostics/classifier-audit-pack.zip')
    def diagnostics_classifier_audit_pack() -> Response:
        from app.services.classifier_audit_pack import build_classifier_audit_pack
        from app.services.evidence_pack import pack_to_zip_bytes

        content = pack_to_zip_bytes(
            build_classifier_audit_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=5,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=classifier_audit_pack_last_5_sessions.zip'},
        )


    @app.get('/diagnostics/outcome-adjudication-pack.zip')
    def diagnostics_outcome_adjudication_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.outcome_adjudication_pack import build_outcome_adjudication_pack

        content = pack_to_zip_bytes(
            build_outcome_adjudication_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=5,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=outcome_adjudication_pack_last_5_sessions.zip'},
        )




    @app.get('/diagnostics/historical-replay-shadow-pack.zip')
    def diagnostics_historical_replay_shadow_pack() -> Response:
        from app.services.historical_replay_shadow_pack import get_or_build_historical_replay_shadow_zip

        content = get_or_build_historical_replay_shadow_zip(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            lookback_days=90,
            offsets=[120, 150],
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=historical_replay_shadow_pack_last_90_days.zip'},
        )

    @app.get('/diagnostics/replay-bottleneck-pack.zip')
    def diagnostics_replay_bottleneck_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.replay_bottleneck_pack import build_replay_bottleneck_pack

        content = pack_to_zip_bytes(
            build_replay_bottleneck_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                lookback_days=90,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=replay_bottleneck_pack_last_90_days.zip'},
        )

    @app.get('/diagnostics/replay-checkpoint-decay-pack.zip')
    def diagnostics_replay_checkpoint_decay_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.replay_checkpoint_decay_pack import build_replay_checkpoint_decay_pack

        content = pack_to_zip_bytes(
            build_replay_checkpoint_decay_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                lookback_days=90,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=replay_checkpoint_decay_pack_last_90_days.zip'},
        )


    @app.get('/diagnostics/replay-checkpoint-compatibility-pack.zip')
    def diagnostics_replay_checkpoint_compatibility_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.replay_checkpoint_compatibility_pack import build_replay_checkpoint_compatibility_pack

        content = pack_to_zip_bytes(
            build_replay_checkpoint_compatibility_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                lookback_days=90,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=replay_checkpoint_compatibility_pack_last_90_days.zip'},
        )


    @app.get('/diagnostics/weaker-checkpoint-secondary-gate-pack.zip')
    def diagnostics_weaker_checkpoint_secondary_gate_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.weaker_checkpoint_secondary_gate_pack import build_weaker_checkpoint_secondary_gate_pack

        content = pack_to_zip_bytes(
        build_weaker_checkpoint_secondary_gate_pack(
        runtime.settings,
        runtime.db,
        runtime.alpaca,
        lookback_days=90,
        offsets=[120, 150],
        )
        )
        return Response(
        content=content,
        media_type='application/zip',
        headers={'Content-Disposition': 'attachment; filename=weaker_checkpoint_secondary_gate_pack_last_90_days.zip'},
        )

    @app.get('/diagnostics/weaker-checkpoint-gate-shadow-pack.zip')
    def diagnostics_weaker_checkpoint_gate_shadow_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.weaker_checkpoint_gate_shadow_pack import build_weaker_checkpoint_gate_shadow_pack

        content = pack_to_zip_bytes(
            build_weaker_checkpoint_gate_shadow_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=weaker_checkpoint_gate_shadow_pack_latest_day.zip'},
        )


    @app.get('/diagnostics/overstrictness-shadow-pack.zip')
    def diagnostics_overstrictness_shadow_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.overstrictness_shadow_pack import build_overstrictness_shadow_pack

        content = pack_to_zip_bytes(
            build_overstrictness_shadow_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=10,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=overstrictness_shadow_pack_last_10_sessions.zip'},
        )

    @app.get('/diagnostics/replay-supported-visual-review-pack.zip')
    def diagnostics_replay_supported_visual_review_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.replay_supported_visual_review_pack import build_replay_supported_visual_review_pack

        content = pack_to_zip_bytes(
            build_replay_supported_visual_review_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=10,
                offsets=[120, 150],
                lookback_days=90,
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=replay_supported_visual_review_pack_last_10_sessions.zip'},
        )

    @app.get('/diagnostics/shadow-visual-review-pack.zip')
    def diagnostics_shadow_visual_review_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.shadow_visual_review_pack import build_shadow_visual_review_pack

        content = pack_to_zip_bytes(
            build_shadow_visual_review_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=10,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=shadow_visual_review_pack_last_10_sessions.zip'},
        )

    @app.get('/diagnostics/shadow-promotion-pack.zip')
    def diagnostics_shadow_promotion_pack() -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.shadow_promotion_pack import build_shadow_promotion_pack

        content = pack_to_zip_bytes(
            build_shadow_promotion_pack(
                runtime.settings,
                runtime.db,
                runtime.alpaca,
                days=10,
                offsets=[120, 150],
            )
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=shadow_promotion_pack_last_10_sessions.zip'},
        )


    @app.get('/diagnostics/decision-bundle.zip')
    def diagnostics_decision_bundle() -> Response:
        from app.services.decision_bundle import get_or_build_decision_bundle_zip

        content = get_or_build_decision_bundle_zip(
            runtime.settings,
            runtime.db,
            runtime.alpaca,
            days=60,
            offsets=[120, 150],
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': 'attachment; filename=decision_bundle_last_60_sessions.zip'},
        )


    @app.post('/diagnostics/refresh-universe')
    def diagnostics_refresh_universe() -> RedirectResponse:
        from app.services.universe import load_universe
        load_universe(runtime.settings, runtime.db, force_refresh=True)
        return RedirectResponse(url='/diagnostics', status_code=303)

    @app.post('/diagnostics/evaluate-live-outcomes')
    def diagnostics_evaluate_live_outcomes() -> RedirectResponse:
        from app.services.live_trust import evaluate_pending_live_outcomes
        evaluate_pending_live_outcomes(runtime.settings, runtime.db, runtime.alpaca)
        return RedirectResponse(url='/diagnostics', status_code=303)
