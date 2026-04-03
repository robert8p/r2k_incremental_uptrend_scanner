"""
Authentication middleware for the R2K scanner.

Adds bearer-token protection to all mutating routes (POST endpoints,
settings, trade submission, research, validation). Read-only GET routes
like healthz, index, and scan detail remain open so the dashboard is
still viewable without auth during development.

Setup:
  1. Set AUTH_TOKEN in .env or environment
  2. All POST routes and sensitive GETs require:
       Authorization: Bearer <token>
     or query parameter ?token=<token> (for browser form submissions)

If AUTH_TOKEN is empty or unset, auth is disabled (development mode).
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Routes that never require auth (read-only operational endpoints)
OPEN_ROUTES: set[str] = {
    '/healthz',
    '/status',
    '/docs',
    '/openapi.json',
}

# Route prefixes that are read-only and safe without auth
OPEN_GET_PREFIXES: tuple[str, ...] = (
    '/static/',
)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """
    Simple bearer-token authentication middleware.

    - If no AUTH_TOKEN is configured, all requests pass through (dev mode).
    - GET requests to non-sensitive routes pass through.
    - All POST requests require a valid token.
    - Sensitive GET routes (settings, diagnostics) require a valid token.
    """

    def __init__(self, app, auth_token: str = ''):
        super().__init__(app)
        self.auth_token = auth_token.strip()
        self.enabled = bool(self.auth_token)
        if not self.enabled:
            logger.warning(
                'AUTH_TOKEN is not set. Authentication is DISABLED. '
                'Set AUTH_TOKEN in your environment to protect mutating routes.'
            )
        else:
            logger.info('Token authentication enabled for mutating routes.')

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path.rstrip('/')
        method = request.method.upper()

        # Always allow open routes
        if path in OPEN_ROUTES or any(path.startswith(p) for p in OPEN_GET_PREFIXES):
            return await call_next(request)

        # GET requests to read-only pages pass through (dashboard viewing)
        # POST requests always require auth
        if method == 'GET' and not self._is_sensitive_get(path):
            return await call_next(request)

        # Check token
        token = self._extract_token(request)
        if not token or not secrets.compare_digest(token, self.auth_token):
            logger.warning('Auth rejected: %s %s from %s', method, path, request.client.host if request.client else 'unknown')
            return JSONResponse(
                status_code=401,
                content={'detail': 'Unauthorized. Provide a valid token.'},
            )

        return await call_next(request)

    def _is_sensitive_get(self, path: str) -> bool:
        """GETs that expose operational controls or sensitive data."""
        sensitive_patterns = (
            '/settings',
            '/diagnostics',
            '/api/research/',
        )
        return any(path.startswith(p) or path == p for p in sensitive_patterns)

    def _extract_token(self, request: Request) -> Optional[str]:
        # Check Authorization header first
        auth_header = request.headers.get('authorization', '')
        if auth_header.lower().startswith('bearer '):
            return auth_header[7:].strip()

        # Fallback: check query parameter (for browser form submissions)
        token = request.query_params.get('token', '').strip()
        if token:
            return token

        # Fallback: check form field (for HTML form POST submissions)
        # Note: This requires reading the body, so we check cookies/headers first
        return None
