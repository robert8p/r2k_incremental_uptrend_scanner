from pathlib import Path


def test_backtest_imports_timezone_for_validation_timestamp_regression():
    source = Path('app/services/backtest.py').read_text(encoding='utf-8')
    assert 'from datetime import datetime, timedelta, timezone' in source
