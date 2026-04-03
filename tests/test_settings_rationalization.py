from __future__ import annotations

import json

from app.config import OVERRIDABLE_FIELDS, Settings, load_settings, save_settings_override


def test_policy_snapshot_groups_settings_cleanly():
    settings = Settings()
    groups = settings.policy_snapshot()

    assert 'product_policy' in groups
    assert 'scoring_policy' in groups
    assert 'research_policy' in groups
    assert 'operational_policy' in groups
    assert groups['product_policy']['target_pct'] == settings.target_pct
    assert groups['operational_policy']['scheduled_offsets'] == settings.scheduled_offsets
    assert groups['scoring_policy']['weights'] == settings.weights


def test_public_snapshot_includes_policy_summary_and_groups():
    settings = Settings()
    snap = settings.public_snapshot()

    assert 'policy_groups' in snap
    assert 'policy_summary' in snap
    assert 'overrideable_fields' in snap
    assert 'product_policy' in snap['policy_groups']
    assert sorted(OVERRIDABLE_FIELDS) == snap['overrideable_fields']


def test_save_settings_override_uses_overrideable_field_allowlist(tmp_path):
    settings = Settings(settings_override_path=str(tmp_path / 'override.json'))
    save_settings_override(settings, {'target_pct': 1.25, 'app_name': 'Nope', 'weight_liquidity': 0.22})

    payload = json.loads((tmp_path / 'override.json').read_text(encoding='utf-8'))
    assert payload['target_pct'] == 1.25
    assert payload['weight_liquidity'] == 0.22
    assert 'app_name' not in payload



def test_save_settings_override_cleans_stale_disallowed_keys(tmp_path):
    override_path = tmp_path / 'override.json'
    override_path.write_text(json.dumps({'app_env': 'development', 'target_pct': 1.1}), encoding='utf-8')

    settings = Settings(settings_override_path=str(override_path))
    save_settings_override(settings, {'weight_liquidity': 0.22})

    payload = json.loads(override_path.read_text(encoding='utf-8'))
    assert payload == {'target_pct': 1.1, 'weight_liquidity': 0.22}
    assert 'app_env' not in payload


def test_load_settings_ignores_stale_disallowed_override_keys(tmp_path, monkeypatch):
    override_path = tmp_path / 'override.json'
    override_path.write_text(json.dumps({'app_env': 'development', 'min_price': 1.75}), encoding='utf-8')

    monkeypatch.setenv('APP_ENV', 'production')
    monkeypatch.setenv('SETTINGS_OVERRIDE_PATH', str(override_path))

    settings = load_settings()

    assert settings.app_env == 'production'
    assert settings.min_price == 1.75
    payload = json.loads(override_path.read_text(encoding='utf-8'))
    assert payload == {'min_price': 1.75}
