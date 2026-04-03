from app.services.live_confirmation_pack import build_live_confirmation_pack
from app.services.evidence_pack import pack_to_zip_bytes


class _FakeStatus:
    def __init__(self):
        self.__dict__.update({
            'source': 'IWM holdings proxy',
            'raw_count': 1941,
            'tradable_count': 1926,
            'cache_age_hours': 0.5,
            'note': 'test',
        })


class _FakeDB:
    def get_latest_scan(self):
        return {'id': 2, 'trading_day': '2026-03-27', 'scan_offset_minutes': 150, 'status': 'ok', 'created_at': 'now', 'summary': {}}

    def list_validation_runs(self, limit=1):
        return []

    def list_research_runs(self, limit=1):
        return []


class _FakeScanRepo:
    def list_recent(self, limit=500):
        return [
            {'id': 2, 'trading_day': '2026-03-27', 'scan_offset_minutes': 150, 'stage1_count': 50, 'summary': {}, 'status': 'ok', 'mode': 'scan_only', 'created_at': 'now', 'scan_timestamp': 'ts', 'universe_count': 100, 'stage2_count': 1},
            {'id': 1, 'trading_day': '2026-03-27', 'scan_offset_minutes': 120, 'stage1_count': 50, 'summary': {}, 'status': 'ok', 'mode': 'scan_only', 'created_at': 'now', 'scan_timestamp': 'ts', 'universe_count': 100, 'stage2_count': 0},
        ]

    def get_candidates(self, scan_id):
        if scan_id == 2:
            return [
                {'symbol': 'AAA', 'advanced_to_stage2': True, 'total_score': 70.0, 'exclusion_reason': None},
                {'symbol': 'BBB', 'advanced_to_stage2': False, 'exclusion_reason': 'Average daily dollar volume below threshold.'},
            ]
        return [
            {'symbol': 'CCC', 'advanced_to_stage2': False, 'exclusion_reason': 'Below preferred minimum price.'},
        ]


class _FakeValidationRepo:
    def list_recent(self, limit=1):
        return []


class _FakeResearchRepo:
    def list_recent(self, limit=20):
        return []


class _FakeRepos:
    def __init__(self):
        self.db = _FakeDB()
        self.scan = _FakeScanRepo()
        self.validation = _FakeValidationRepo()
        self.research = _FakeResearchRepo()


class _FakeSettings:
    alpaca_data_feed = 'sip'
    app_name = 'Test App'
    app_env = 'development'
    trading_mode = 'scan_only'
    enable_live_trading = False
    auth_enabled = True
    auth_token = 'x'
    default_notional_usd = 1000.0
    default_scan_offset_minutes = 150
    scheduled_offsets = [120, 150]
    data_dir = './data'
    universe_cache_ttl_hours = 24

    def public_snapshot(self):
        return {'default_scan_offset_minutes': 150, 'scheduled_offsets': [120, 150]}


class _FakeAlpaca:
    def ping_data_api(self):
        return {'message': 'ok'}

    def has_credentials(self):
        return True


def test_build_live_confirmation_pack_contains_expected_files(monkeypatch):
    fake_repos = _FakeRepos()
    monkeypatch.setattr('app.services.live_confirmation_pack.ensure_repository_bundle', lambda source: fake_repos)
    monkeypatch.setattr('app.services.live_confirmation_pack.load_universe', lambda settings, db, force_refresh=False: {'status': _FakeStatus()})
    monkeypatch.setattr('app.services.live_confirmation_pack.build_diagnostics_snapshot', lambda settings, db, alpaca, scheduler_status=None: {'ok': True})
    monkeypatch.setattr('app.services.live_confirmation_pack.build_live_trust_snapshot', lambda settings, db: {'status_counts': {'pending': 0, 'evaluated': 0}})
    monkeypatch.setattr('app.services.live_confirmation_pack.build_job_status_snapshot', lambda db: {'latest_scan': {'id': 2}})
    monkeypatch.setattr('app.services.live_confirmation_pack.build_contract_health', lambda db: {'ok': True})
    pack = build_live_confirmation_pack(_FakeSettings(), _FakeDB(), _FakeAlpaca(), days=5, offsets=[120, 150], scheduler_status={'leader': True})
    assert 'MANIFEST.json' in pack
    assert 'live_trust.json' in pack
    assert 'jobs_snapshot.json' in pack
    assert 'scan_rollup.csv' in pack
    assert 'live_confirmation/2026-03-27/offset_150_scan_2_summary.json' in pack
    content = pack_to_zip_bytes(pack)
    assert content[:2] == b'PK'
