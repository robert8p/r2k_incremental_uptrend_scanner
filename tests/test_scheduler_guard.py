from app.services.scheduler_guard import SchedulerLeaderGuard


def test_scheduler_guard_allows_only_one_leader_per_lock(tmp_path):
    lock_path = tmp_path / 'scheduler.lock'
    first = SchedulerLeaderGuard(str(lock_path))
    second = SchedulerLeaderGuard(str(lock_path))

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()

    third = SchedulerLeaderGuard(str(lock_path))
    assert third.acquire() is True
    third.release()
