from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

try:  # pragma: no cover - unix path exercised in tests/runtime
    import fcntl  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    fcntl = None  # type: ignore


class SchedulerLeaderGuard:
    """Process-level leader election for the in-process scheduler.

    Uses a non-blocking file lock on the shared data disk so only one web
    process starts APScheduler even if the platform launches multiple server
    processes.
    """

    def __init__(self, lock_path: str):
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._leader = False
        self._metadata: Dict[str, Any] = {
            'lock_path': str(self.lock_path),
            'leader': False,
            'supported': fcntl is not None,
            'pid': os.getpid(),
        }

    def acquire(self) -> bool:
        self._metadata['acquired_at'] = datetime.now(timezone.utc).isoformat()
        self._metadata['pid'] = os.getpid()
        if fcntl is None:
            logger.warning('fcntl unavailable; scheduler leader lock not supported on this platform.')
            self._leader = True
            self._metadata['leader'] = True
            self._metadata['mode'] = 'unsupported_platform_fallback'
            return True
        self._fh = self.lock_path.open('a+', encoding='utf-8')
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._leader = True
            self._metadata['leader'] = True
            self._metadata['mode'] = 'file_lock_leader'
            self._write_metadata(status='leader')
            logger.info('Scheduler leader lock acquired: %s', self.lock_path)
            return True
        except BlockingIOError:
            self._leader = False
            self._metadata['leader'] = False
            self._metadata['mode'] = 'file_lock_follower'
            self._metadata['status'] = 'follower'
            logger.warning('Scheduler leader lock already held; this process will not start APScheduler.')
            return False

    def release(self) -> None:
        if not self._fh:
            return
        try:
            if self._leader and fcntl is not None:
                self._write_metadata(status='released')
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._leader = False
            self._metadata['leader'] = False
            self._metadata['released_at'] = datetime.now(timezone.utc).isoformat()
            self._fh.close()
            self._fh = None

    def status(self, *, scheduler_running: bool = False) -> Dict[str, Any]:
        payload = dict(self._metadata)
        payload['scheduler_running'] = bool(scheduler_running)
        return payload

    def _write_metadata(self, *, status: str) -> None:
        if not self._fh:
            return
        payload = {
            'status': status,
            'pid': os.getpid(),
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'lock_path': str(self.lock_path),
        }
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(json.dumps(payload))
        self._fh.flush()
        os.fsync(self._fh.fileno())
