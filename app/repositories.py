from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.db import Database


class ScanRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_recent(self, limit: int = 20):
        return self.db.list_scans(limit=limit)

    def get_latest(self):
        return self.db.get_latest_scan()

    def get(self, scan_id: int):
        return self.db.get_scan(scan_id)

    def get_candidates(self, scan_id: int):
        return self.db.get_scan_candidates(scan_id)

    def get_candidate(self, scan_id: int, symbol: str):
        return self.db.get_candidate(scan_id, symbol)

    def insert(self, scan_payload, candidates):
        return self.db.insert_scan(scan_payload, candidates)


class ValidationRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_recent(self, limit: int = 20):
        return self.db.list_validation_runs(limit=limit)

    def get(self, validation_id: int):
        return self.db.get_validation_run(validation_id)

    def insert(self, payload, rows):
        return self.db.insert_validation_run(payload, rows)


class ResearchRepository:
    def __init__(self, db: Database):
        self.db = db

    def list_recent(self, limit: int = 20):
        return self.db.list_research_runs(limit=limit)

    def get(self, run_id: int):
        return self.db.get_research_run(run_id)

    def insert(self, params, *, status='queued', message=''):
        return self.db.insert_research_run(params, status=status, message=message)

    def update(self, run_id: int, **kwargs):
        return self.db.update_research_run(run_id, **kwargs)


@dataclass
class RepositoryBundle:
    db: Database

    def __post_init__(self):
        self.scan = ScanRepository(self.db)
        self.validation = ValidationRepository(self.db)
        self.research = ResearchRepository(self.db)


def ensure_repository_bundle(source: Database | RepositoryBundle) -> RepositoryBundle:
    if isinstance(source, RepositoryBundle):
        return source
    return RepositoryBundle(source)
