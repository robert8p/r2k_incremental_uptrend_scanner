#!/usr/bin/env bash
set -euo pipefail
uvicorn app.main:app --host "${APP_HOST:-0.0.0.0}" --port "${PORT:-${APP_PORT:-8000}}" --workers 1
