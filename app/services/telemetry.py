from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger('app.telemetry')


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return str(value)


def emit_event(event: str, *, level: str = 'info', **fields: Any) -> Dict[str, Any]:
    payload = {
        'event': event,
        'time_utc': datetime.now(timezone.utc).isoformat(),
        **{k: _json_safe(v) for k, v in fields.items()},
    }
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(json.dumps(payload, sort_keys=True))
    return payload
