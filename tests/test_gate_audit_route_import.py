from app.services.research import start_gate_audit_run


def test_start_gate_audit_run_is_importable():
    assert callable(start_gate_audit_run)
