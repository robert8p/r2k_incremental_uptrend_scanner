from __future__ import annotations

from typing import Callable

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.page_contexts import (
    build_candidate_detail_page_context,
    build_index_page_context,
    build_scan_detail_page_context,
)
from app.view_models import build_candidate_view, build_scan_view


def register_scan_routes(
    app: FastAPI,
    *,
    runtime,
    safe_render: Callable,
    build_candidate_chart: Callable,
) -> None:
    @app.get('/', response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        settings = runtime.settings
        context = build_index_page_context(settings, runtime.db)
        from app.services.universe import load_universe
        universe = load_universe(settings, runtime.db, force_refresh=False)
        context['universe_status'] = universe['status']
        return safe_render(request, 'index.html', context)

    @app.get('/scan/recent/export.zip')
    def recent_scan_export_zip(
        days: int = Query(default=5, ge=1, le=20),
        offsets: str = Query(default='120,150'),
    ) -> Response:
        from app.services.evidence_pack import pack_to_zip_bytes
        from app.services.recent_scan_export import build_recent_scan_export_pack

        parsed_offsets = [int(part.strip()) for part in str(offsets).split(',') if part.strip()]
        content = pack_to_zip_bytes(
            build_recent_scan_export_pack(runtime.settings, runtime.db, days=days, offsets=parsed_offsets)
        )
        return Response(
            content=content,
            media_type='application/zip',
            headers={'Content-Disposition': f'attachment; filename=recent_scans_last_{days}_sessions_offsets_{"-".join(str(v) for v in parsed_offsets)}.zip'},
        )

    @app.post('/scan/run')
    def scan_run(offset_minutes: int = Form(...), trading_day: str = Form('')) -> RedirectResponse:
        settings = runtime.settings
        alpaca = runtime.alpaca
        from app.services.market_time import latest_or_previous_trading_day
        from app.services.scanner import run_scan
        chosen_day = trading_day or latest_or_previous_trading_day()
        run_scan(settings, runtime.db, alpaca, trading_day=chosen_day, offset_minutes=offset_minutes)
        latest_scan = runtime.db.get_latest_scan()
        return RedirectResponse(url=f'/scan/{latest_scan["id"]}', status_code=303)

    @app.get('/scan/{scan_id}', response_class=HTMLResponse)
    def scan_detail(request: Request, scan_id: int) -> HTMLResponse:
        settings = runtime.settings
        context = build_scan_detail_page_context(settings, runtime.db, scan_id)
        if not context:
            raise HTTPException(status_code=404, detail='Scan not found.')
        return safe_render(request, 'scan_detail.html', context)

    @app.get('/scan/{scan_id}/candidate/{symbol}', response_class=HTMLResponse)
    def candidate_detail(request: Request, scan_id: int, symbol: str) -> HTMLResponse:
        settings = runtime.settings
        scan = build_scan_view(runtime.db.get_scan(scan_id), alpaca_data_feed=settings.alpaca_data_feed)
        candidate = build_candidate_view(runtime.db.get_candidate(scan_id, symbol))
        if not scan or not candidate:
            raise HTTPException(status_code=404, detail='Candidate not found.')
        chart_html = build_candidate_chart(scan, candidate)
        context = build_candidate_detail_page_context(settings, runtime.db, scan_id, symbol, chart_html=chart_html)
        if not context:
            raise HTTPException(status_code=404, detail='Candidate not found.')
        return safe_render(request, 'candidate_detail.html', context)

    @app.post('/trade/submit')
    def trade_submit(
        scan_id: int = Form(...),
        symbol: str = Form(...),
        mode: str = Form(...),
        notional_usd: float = Form(...),
        limit_price: float = Form(...),
    ) -> RedirectResponse:
        settings = runtime.settings
        alpaca = runtime.alpaca
        if not (settings.auth_token or '').strip():
            raise HTTPException(status_code=403, detail='Trade submission requires AUTH_TOKEN.')
        from app.services.trading import TradingSafetyError, submit_candidate_limit_buy
        try:
            submit_candidate_limit_buy(settings, alpaca, symbol=symbol, notional_usd=notional_usd, entry_limit_price=limit_price, mode=mode)
        except TradingSafetyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return RedirectResponse(url=f'/scan/{scan_id}/candidate/{symbol}', status_code=303)
