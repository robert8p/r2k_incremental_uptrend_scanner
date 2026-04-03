from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.contracts import (
    normalize_candidate_payload,
    normalize_research_result,
    normalize_scan_summary,
    normalize_validation_summary,
)



def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for idx, col in enumerate(cursor.description):
        data[col[0]] = row[idx]
    return data


class Database:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = dict_factory
        return conn

    def init_db(self) -> None:
        conn = self.connect()
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS universe_cache (
                cache_key TEXT PRIMARY KEY,
                loaded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                scan_offset_minutes INTEGER NOT NULL,
                scan_timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                universe_count INTEGER NOT NULL,
                stage1_count INTEGER NOT NULL,
                stage2_count INTEGER NOT NULL,
                summary_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_scans_trading_day ON scans(trading_day, scan_offset_minutes);

            CREATE TABLE IF NOT EXISTS scan_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                company_name TEXT,
                mover_rank INTEGER,
                intraday_pct_gain REAL,
                advanced_to_stage2 INTEGER NOT NULL,
                exclusion_reason TEXT,
                current_price REAL,
                current_cum_volume REAL,
                relative_volume REAL,
                total_score REAL,
                component_scores_json TEXT,
                metrics_json TEXT,
                rationale TEXT,
                entry_low REAL,
                entry_high REAL,
                target_price REAL,
                stretch_target_price REAL,
                stop_price REAL,
                chart_context_json TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id)
            );

            CREATE INDEX IF NOT EXISTS idx_scan_candidates_scan_id ON scan_candidates(scan_id);
            CREATE INDEX IF NOT EXISTS idx_scan_candidates_symbol ON scan_candidates(symbol);

            CREATE TABLE IF NOT EXISTS validation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                scan_offset_minutes INTEGER NOT NULL,
                status TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                rows_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT,
                params_json TEXT NOT NULL,
                result_json TEXT
            );

            CREATE TABLE IF NOT EXISTS live_candidate_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                scan_offset_minutes INTEGER NOT NULL,
                scan_timestamp TEXT NOT NULL,
                recommendation_tier TEXT,
                recommendation_book TEXT,
                advanced_to_stage2 INTEGER NOT NULL,
                total_score REAL,
                entry_low REAL,
                entry_high REAL,
                target_price REAL,
                stop_price REAL,
                evaluated_at TEXT,
                evaluation_status TEXT NOT NULL,
                entry_touched INTEGER,
                hit_target INTEGER,
                minutes_to_entry INTEGER,
                minutes_to_target INTEGER,
                entry_fill_method TEXT,
                target_fill_method TEXT,
                mfe_pct REAL,
                mae_pct REAL,
                end_of_window_return_pct REAL,
                net_end_of_window_return_pct REAL,
                round_trip_cost_bps REAL,
                target_timestamp TEXT,
                error_message TEXT,
                metrics_json TEXT,
                FOREIGN KEY(scan_id) REFERENCES scans(id),
                UNIQUE(scan_id, symbol)
            );

            CREATE INDEX IF NOT EXISTS idx_live_candidate_outcomes_scan_id ON live_candidate_outcomes(scan_id);
            CREATE INDEX IF NOT EXISTS idx_live_candidate_outcomes_status ON live_candidate_outcomes(evaluation_status, trading_day);
            """
        )
        conn.commit()
        conn.close()

    def upsert_universe_cache(self, cache_key: str, loaded_at: str, payload: Dict[str, Any]) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO universe_cache (cache_key, loaded_at, payload_json)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                loaded_at=excluded.loaded_at,
                payload_json=excluded.payload_json
            """,
            (cache_key, loaded_at, json.dumps(payload)),
        )
        conn.commit()
        conn.close()

    def get_universe_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        row = conn.execute(
            "SELECT cache_key, loaded_at, payload_json FROM universe_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        row['payload'] = json.loads(row['payload_json'])
        return row

    def insert_scan(self, scan: Dict[str, Any], candidates: List[Dict[str, Any]]) -> int:
        normalized_summary = normalize_scan_summary(scan.get('summary'))
        normalized_candidates = [normalize_candidate_payload(candidate) for candidate in candidates]
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO scans (
                created_at, trading_day, scan_offset_minutes, scan_timestamp, status, mode,
                universe_count, stage1_count, stage2_count, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan['created_at'],
                scan['trading_day'],
                scan['scan_offset_minutes'],
                scan['scan_timestamp'],
                scan['status'],
                scan['mode'],
                scan['universe_count'],
                scan['stage1_count'],
                scan['stage2_count'],
                json.dumps(normalized_summary),
            ),
        )
        scan_id = cur.lastrowid
        cur.executemany(
            """
            INSERT INTO scan_candidates (
                scan_id, symbol, company_name, mover_rank, intraday_pct_gain, advanced_to_stage2,
                exclusion_reason, current_price, current_cum_volume, relative_volume, total_score,
                component_scores_json, metrics_json, rationale, entry_low, entry_high,
                target_price, stretch_target_price, stop_price, chart_context_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    scan_id,
                    c['symbol'],
                    c.get('company_name'),
                    c.get('mover_rank'),
                    c.get('intraday_pct_gain'),
                    1 if c.get('advanced_to_stage2') else 0,
                    c.get('exclusion_reason'),
                    c.get('current_price'),
                    c.get('current_cum_volume'),
                    c.get('relative_volume'),
                    c.get('total_score'),
                    json.dumps(c.get('component_scores', {})),
                    json.dumps(c.get('metrics', {})),
                    c.get('rationale'),
                    c.get('entry_low'),
                    c.get('entry_high'),
                    c.get('target_price'),
                    c.get('stretch_target_price'),
                    c.get('stop_price'),
                    json.dumps(c.get('chart_context', {})),
                )
                for c in normalized_candidates
            ],
        )
        advanced_candidates = [c for c in normalized_candidates if c.get('advanced_to_stage2')]
        if advanced_candidates:
            cur.executemany(
                """
                INSERT OR IGNORE INTO live_candidate_outcomes (
                    scan_id, symbol, trading_day, scan_offset_minutes, scan_timestamp,
                    recommendation_tier, recommendation_book, advanced_to_stage2, total_score,
                    entry_low, entry_high, target_price, stop_price,
                    evaluated_at, evaluation_status, entry_touched, hit_target,
                    minutes_to_entry, minutes_to_target, entry_fill_method, target_fill_method,
                    mfe_pct, mae_pct, end_of_window_return_pct, net_end_of_window_return_pct,
                    round_trip_cost_bps, target_timestamp, error_message, metrics_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending', NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?)
                """,
                [
                    (
                        scan_id,
                        c['symbol'],
                        scan['trading_day'],
                        scan['scan_offset_minutes'],
                        scan['scan_timestamp'],
                        c.get('recommendation_tier'),
                        c.get('recommendation_book'),
                        1,
                        c.get('total_score'),
                        c.get('entry_low'),
                        c.get('entry_high'),
                        c.get('target_price'),
                        c.get('stop_price'),
                        json.dumps(c.get('metrics', {})),
                    )
                    for c in advanced_candidates
                ],
            )
        conn.commit()
        conn.close()
        return int(scan_id)

    def list_scans(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM scans
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        for row in rows:
            row['summary'] = normalize_scan_summary(json.loads(row['summary_json']))
        return rows

    def get_scan(self, scan_id: int) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        row = conn.execute('SELECT * FROM scans WHERE id = ?', (scan_id,)).fetchone()
        conn.close()
        if not row:
            return None
        row['summary'] = normalize_scan_summary(json.loads(row['summary_json']))
        return row

    def get_latest_scan(self) -> Optional[Dict[str, Any]]:
        scans = self.list_scans(limit=1)
        return scans[0] if scans else None

    def get_scan_candidates(self, scan_id: int) -> List[Dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM scan_candidates
            WHERE scan_id = ?
            ORDER BY mover_rank ASC, COALESCE(total_score, -1) DESC
            """,
            (scan_id,),
        ).fetchall()
        conn.close()
        normalized_rows = []
        for row in rows:
            row['component_scores'] = json.loads(row['component_scores_json'] or '{}')
            row['metrics'] = json.loads(row['metrics_json'] or '{}')
            row['chart_context'] = json.loads(row['chart_context_json'] or '{}')
            normalized_rows.append(normalize_candidate_payload(row))
        return normalized_rows

    def get_candidate(self, scan_id: int, symbol: str) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        row = conn.execute(
            """
            SELECT * FROM scan_candidates
            WHERE scan_id = ? AND symbol = ?
            """,
            (scan_id, symbol),
        ).fetchone()
        conn.close()
        if not row:
            return None
        row['component_scores'] = json.loads(row['component_scores_json'] or '{}')
        row['metrics'] = json.loads(row['metrics_json'] or '{}')
        row['chart_context'] = json.loads(row['chart_context_json'] or '{}')
        return normalize_candidate_payload(row)

    def list_live_candidate_outcomes(self, *, limit: int = 200, status: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self.connect()
        if status is None:
            rows = conn.execute(
                """
                SELECT * FROM live_candidate_outcomes
                ORDER BY trading_day DESC, scan_id DESC, COALESCE(total_score, -1) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM live_candidate_outcomes
                WHERE evaluation_status = ?
                ORDER BY trading_day ASC, scan_id ASC, COALESCE(total_score, -1) DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        conn.close()
        return [self._normalize_live_candidate_outcome(row) for row in rows]

    def list_live_candidate_outcomes_for_scan(self, scan_id: int) -> List[Dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM live_candidate_outcomes
            WHERE scan_id = ?
            ORDER BY COALESCE(total_score, -1) DESC, symbol ASC
            """,
            (scan_id,),
        ).fetchall()
        conn.close()
        return [self._normalize_live_candidate_outcome(row) for row in rows]

    def update_live_candidate_outcome(self, scan_id: int, symbol: str, **kwargs) -> None:
        fields = []
        values: List[Any] = []
        for key, value in kwargs.items():
            if key == 'metrics' and value is not None:
                fields.append('metrics_json = ?')
                values.append(json.dumps(value))
                continue
            fields.append(f'{key} = ?')
            values.append(value)
        if not fields:
            return
        values.extend([scan_id, symbol])
        conn = self.connect()
        conn.execute(
            f"UPDATE live_candidate_outcomes SET {', '.join(fields)} WHERE scan_id = ? AND symbol = ?",
            values,
        )
        conn.commit()
        conn.close()

    def _normalize_live_candidate_outcome(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(row)
        normalized['advanced_to_stage2'] = bool(normalized.get('advanced_to_stage2'))
        if normalized.get('entry_touched') is not None:
            normalized['entry_touched'] = bool(normalized.get('entry_touched'))
        if normalized.get('hit_target') is not None:
            normalized['hit_target'] = bool(normalized.get('hit_target'))
        normalized['metrics'] = json.loads(normalized.get('metrics_json') or '{}')
        return normalized

    def insert_validation_run(self, payload: Dict[str, Any], rows: List[Dict[str, Any]]) -> int:
        normalized_summary = normalize_validation_summary(payload.get('summary'))
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO validation_runs (
                created_at, start_date, end_date, scan_offset_minutes, status,
                summary_json, rows_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload['created_at'],
                payload['start_date'],
                payload['end_date'],
                payload['scan_offset_minutes'],
                payload['status'],
                json.dumps(normalized_summary),
                json.dumps(rows),
            ),
        )
        validation_id = cur.lastrowid
        conn.commit()
        conn.close()
        return int(validation_id)

    def list_validation_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM validation_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        for row in rows:
            row['summary'] = normalize_validation_summary(json.loads(row['summary_json']))
        return rows

    def get_validation_run(self, validation_id: int) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        row = conn.execute(
            'SELECT * FROM validation_runs WHERE id = ?',
            (validation_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        row['summary'] = normalize_validation_summary(json.loads(row['summary_json']))
        row['rows'] = json.loads(row['rows_json'])
        return row

    def insert_research_run(self, params: Dict[str, Any], *, status: str = 'queued', message: str = '') -> int:
        conn = self.connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO research_runs (
                created_at, started_at, finished_at, status, progress, message, params_json, result_json
            ) VALUES (?, NULL, NULL, ?, 0, ?, ?, NULL)
            """,
            (
                params.get('created_at'),
                status,
                message,
                json.dumps(params),
            ),
        )
        run_id = cur.lastrowid
        conn.commit()
        conn.close()
        return int(run_id)

    def update_research_run(
        self,
        run_id: int,
        *,
        status: Optional[str] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        fields = []
        values: List[Any] = []
        if status is not None:
            fields.append('status = ?')
            values.append(status)
        if progress is not None:
            fields.append('progress = ?')
            values.append(progress)
        if message is not None:
            fields.append('message = ?')
            values.append(message)
        if started_at is not None:
            fields.append('started_at = ?')
            values.append(started_at)
        if finished_at is not None:
            fields.append('finished_at = ?')
            values.append(finished_at)
        if result is not None:
            fields.append('result_json = ?')
            values.append(json.dumps(normalize_research_result(result)))
        if not fields:
            return
        values.append(run_id)
        conn = self.connect()
        conn.execute(f"UPDATE research_runs SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()

    def list_research_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT * FROM research_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        conn.close()
        for row in rows:
            row['params'] = json.loads(row['params_json'])
            row['result'] = normalize_research_result(json.loads(row['result_json'])) if row.get('result_json') else None
        return rows

    def get_research_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self.connect()
        row = conn.execute('SELECT * FROM research_runs WHERE id = ?', (run_id,)).fetchone()
        conn.close()
        if not row:
            return None
        row['params'] = json.loads(row['params_json'])
        row['result'] = normalize_research_result(json.loads(row['result_json'])) if row.get('result_json') else None
        return row
