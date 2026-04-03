from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.page_contexts import (
    build_research_detail_page_context,
    build_validation_detail_page_context,
    build_validation_page_context,
)
from app.view_models import build_validation_view



def register_validation_routes(
    app: FastAPI,
    *,
    runtime,
    safe_render: Callable,
    validation_chart_html: Callable,
) -> None:
    @app.get('/validation', response_class=HTMLResponse)
    def validation_page(request: Request) -> HTMLResponse:
        return safe_render(request, 'validation.html', build_validation_page_context(runtime.settings, runtime.db))

    @app.post('/validation/run')
    def validation_run(start_date: str = Form(...), end_date: str = Form(...), scan_offset_minutes: int = Form(...)) -> RedirectResponse:
        settings = runtime.settings
        alpaca = runtime.alpaca
        from app.services.validation_engine import ValidationRunRequest, execute_validation_run
        payload = execute_validation_run(
            settings,
            runtime.db,
            alpaca,
            ValidationRunRequest(
                start_date=start_date,
                end_date=end_date,
                scan_offset_minutes=scan_offset_minutes,
            ),
        )
        return RedirectResponse(url=f"/validation/{payload['id']}", status_code=303)

    @app.post('/validation/research/run')
    def validation_research_run(
        end_date: str = Form(...),
        scan_offset_minutes: int = Form(...),
        apply_recommended_weights: str = Form(''),
        run_offset_ladder: str = Form(''),
        offset_ladder: str = Form(''),
        auto_apply_recommended_schedule: str = Form(''),
    ) -> RedirectResponse:
        from app.services.research import start_three_month_research_run
        run_id = start_three_month_research_run(
            runtime.settings,
            runtime.db,
            scan_offset_minutes=scan_offset_minutes,
            end_date=end_date,
            apply_recommended_weights=bool(apply_recommended_weights),
            run_offset_ladder=bool(run_offset_ladder),
            offset_ladder=offset_ladder,
            auto_apply_recommended_schedule=bool(auto_apply_recommended_schedule),
        )
        return RedirectResponse(url=f'/validation/research/{run_id}', status_code=303)



    @app.post('/validation/research/run-goal-seek')
    def validation_goal_seek_run(
        start_date: str = Form(...),
        end_date: str = Form(...),
        train_days: int = Form(...),
        test_days: int = Form(...),
        step_days: int = Form(...),
        embargo_days: int = Form(...),
        offsets: str = Form('120,150'),
        config_scope: str = Form('full'),
    ) -> RedirectResponse:
        from app.services.research import start_goal_seek_run
        try:
            run_id = start_goal_seek_run(
                runtime.settings,
                runtime.db,
                start_date=start_date,
                end_date=end_date,
                train_days=train_days,
                test_days=test_days,
                step_days=step_days,
                embargo_days=embargo_days,
                offsets=offsets,
                config_scope=config_scope,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f'Goal-seek start failed: {type(exc).__name__}: {exc}') from exc
        return RedirectResponse(url=f'/validation/research/{run_id}', status_code=303)

    @app.post('/validation/research/run-gate-audit')
    def validation_gate_audit_run(
        start_date: str = Form(...),
        end_date: str = Form(...),
        scan_offset_minutes: int = Form(...),
        scenario_a_name: str = Form(...),
        scenario_a_min_avg_dollar_volume: float = Form(...),
        scenario_a_min_price: float = Form(...),
        scenario_a_low_price_hard_floor: float = Form(...),
        scenario_b_name: str = Form(...),
        scenario_b_min_avg_dollar_volume: float = Form(...),
        scenario_b_min_price: float = Form(...),
        scenario_b_low_price_hard_floor: float = Form(...),
    ) -> RedirectResponse:
        from app.services.research import start_gate_audit_run
        try:
            run_id = start_gate_audit_run(
                runtime.settings,
                runtime.db,
                start_date=start_date,
                end_date=end_date,
                scan_offset_minutes=scan_offset_minutes,
                scenario_a_name=scenario_a_name,
                scenario_a_min_avg_dollar_volume=scenario_a_min_avg_dollar_volume,
                scenario_a_min_price=scenario_a_min_price,
                scenario_a_low_price_hard_floor=scenario_a_low_price_hard_floor,
                scenario_b_name=scenario_b_name,
                scenario_b_min_avg_dollar_volume=scenario_b_min_avg_dollar_volume,
                scenario_b_min_price=scenario_b_min_price,
                scenario_b_low_price_hard_floor=scenario_b_low_price_hard_floor,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f'Gate audit start failed: {type(exc).__name__}: {exc}') from exc
        return RedirectResponse(url=f'/validation/research/{run_id}', status_code=303)

    @app.post('/validation/research/{run_id}/apply-calibration')
    def validation_apply_calibration(run_id: int) -> RedirectResponse:
        from app.services.research import apply_recommended_calibration
        apply_recommended_calibration(runtime.settings, runtime.db, run_id)
        runtime.refresh()
        return RedirectResponse(url=f'/validation/research/{run_id}', status_code=303)

    @app.post('/validation/research/{run_id}/apply-schedule')
    def validation_apply_schedule(run_id: int) -> RedirectResponse:
        from app.services.research import apply_recommended_schedule
        apply_recommended_schedule(runtime.settings, runtime.db, run_id)
        runtime.refresh()
        return RedirectResponse(url=f'/validation/research/{run_id}', status_code=303)

    @app.get('/validation/{validation_id}', response_class=HTMLResponse)
    def validation_detail(request: Request, validation_id: int) -> HTMLResponse:
        context = build_validation_detail_page_context(runtime.settings, runtime.db, validation_id, chart_html_builder=validation_chart_html)
        if not context:
            raise HTTPException(status_code=404, detail='Validation run not found.')
        return safe_render(request, 'validation.html', context)


    @app.get('/validation/{validation_id}/evidence.zip')
    def validation_evidence_zip(validation_id: int) -> Response:
        from app.services.evidence_pack import build_validation_evidence_pack, pack_to_zip_bytes
        content = pack_to_zip_bytes(build_validation_evidence_pack(runtime.settings, runtime.db, validation_id))
        return Response(content=content, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename=validation_{validation_id}_evidence.zip'})

    @app.get('/validation/research/{run_id}', response_class=HTMLResponse)
    def validation_research_detail(request: Request, run_id: int) -> HTMLResponse:
        context = build_research_detail_page_context(runtime.settings, runtime.db, run_id, chart_html_builder=validation_chart_html)
        if not context:
            raise HTTPException(status_code=404, detail='Research run not found.')
        return safe_render(request, 'validation.html', context)


    @app.get('/validation/research/{run_id}/evidence.zip')
    def research_evidence_zip(run_id: int) -> Response:
        from app.services.evidence_pack import build_research_evidence_pack, pack_to_zip_bytes
        content = pack_to_zip_bytes(build_research_evidence_pack(runtime.settings, runtime.db, run_id))
        return Response(content=content, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename=research_{run_id}_evidence.zip'})

    @app.get('/validation/{validation_id}/summary.json')
    def validation_summary_json(validation_id: int) -> JSONResponse:
        validation = build_validation_view(runtime.db.get_validation_run(validation_id))
        if not validation:
            raise HTTPException(status_code=404, detail='Validation run not found.')
        return JSONResponse(validation)

    @app.get('/validation/{validation_id}/rows.csv')
    def validation_rows_csv(validation_id: int) -> Response:
        validation = build_validation_view(runtime.db.get_validation_run(validation_id))
        if not validation:
            raise HTTPException(status_code=404, detail='Validation run not found.')
        from app.services.backtest import validation_rows_to_csv
        content = validation_rows_to_csv(validation['rows'])
        return Response(content=content, media_type='text/csv', headers={'Content-Disposition': f'attachment; filename=validation_{validation_id}.csv'})
