from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic, sleep

import pytest

from agentbridge.core.draft_read_scheduler import DraftReadBusy, DraftReadScheduler, SharedDraftReadFailure


def wait_until(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, "scheduler did not reach the expected state"
        sleep(0.005)


def test_busy_execution_session_does_not_consume_global_read_slots():
    scheduler = DraftReadScheduler(wait_timeout=5)
    mutex = Lock()
    mutex.acquire()  # Existing business execution owns this session.
    release = Event()
    entered_b, entered_c, entered_a = Event(), Event(), Event()

    def read(key, lock, entered):
        with scheduler.slot((key,), lock):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(read, "a", mutex, entered_a)
        wait_until(lambda: scheduler.status()["queued"] == 1)
        second = pool.submit(read, "b", Lock(), entered_b)
        third = pool.submit(read, "c", Lock(), entered_c)
        try:
            assert entered_b.wait(3) and entered_c.wait(3)
            assert scheduler.status()["active"] == 2
            assert not entered_a.is_set()
        finally:
            release.set()
            mutex.release()
        for result in (first, second, third):
            result.result(5)
    assert scheduler.status() == {"active": 0, "queued": 0, "flights": 0, "followers": 0}


def test_rechecks_are_prioritized_without_starving_other_sessions_or_ordinary_previews():
    scheduler = DraftReadScheduler(concurrency=1, wait_timeout=5)
    order = []
    locks = {key: Lock() for key in ("a", "b", "c")}

    def read(label, key, priority=False):
        with scheduler.slot((key,), locks[key], priority=priority):
            order.append(label)

    requests = [("a1", "a", False), ("a2", "a", False), ("b", "b", False),
                ("c1", "c", True), ("c2", "c", True), ("c3", "c", True)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        with scheduler.slot(("blocker",), Lock()):
            futures = []
            for count, request in enumerate(requests, 1):
                futures.append(pool.submit(read, *request))
                wait_until(lambda: scheduler.status()["queued"] == count)
        for future in futures:
            future.result(5)
    assert order == ["c1", "c2", "a1", "c3", "b", "a2"]


def test_queue_limit_and_callback_failure_release_all_resources():
    scheduler = DraftReadScheduler(concurrency=1, max_pending=1, wait_timeout=0.08)
    mutex = Lock()
    mutex.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: scheduler.slot(("busy",), mutex).__enter__())
        wait_until(lambda: scheduler.status()["queued"] == 1)
        with pytest.raises(DraftReadBusy, match="full"):
            with scheduler.slot(("other",), Lock()):
                pytest.fail("full queue admitted a read")
        with pytest.raises(DraftReadBusy, match="expired"):
            pending.result(3)
    mutex.release()
    with pytest.raises(RuntimeError, match="read failed"):
        with scheduler.slot(("busy",), mutex):
            raise RuntimeError("read failed")
    assert not mutex.locked()
    assert scheduler.status()["active"] == scheduler.status()["queued"] == 0


def test_priority_reads_in_one_session_do_not_hide_its_ordinary_preview():
    scheduler = DraftReadScheduler(concurrency=1, wait_timeout=5)
    order = []
    mutex = Lock()

    def read(label, priority):
        with scheduler.slot(("same-session",), mutex, priority=priority):
            order.append(label)

    with ThreadPoolExecutor(max_workers=4) as pool:
        with scheduler.slot(("blocker",), Lock()):
            futures = []
            for count, request in enumerate([("preview", False), ("check1", True), ("check2", True), ("check3", True)], 1):
                futures.append(pool.submit(read, *request))
                wait_until(lambda: scheduler.status()["queued"] == count)
        for future in futures:
            future.result(5)
    assert order == ["check1", "check2", "preview", "check3"]


def test_round_robin_gives_a_waiting_session_its_turn_before_new_arrivals():
    scheduler = DraftReadScheduler(concurrency=1, wait_timeout=5)
    order = []
    first_entered, release_first = Event(), Event()
    second_entered, release_second = Event(), Event()
    mutex = Lock()

    def first():
        with scheduler.slot(("a",), mutex):
            first_entered.set()
            assert release_first.wait(5)

    def read(label, key, lock, entered=None, release=None):
        with scheduler.slot((key,), lock):
            order.append(label)
            if entered:
                entered.set()
                assert release.wait(5)

    with ThreadPoolExecutor(max_workers=4) as pool:
        initial = pool.submit(first)
        assert first_entered.wait(3)
        waiting = pool.submit(read, "a-next", "a", mutex)
        wait_until(lambda: scheduler.status()["queued"] == 1)
        second = pool.submit(read, "b", "b", Lock(), second_entered, release_second)
        wait_until(lambda: scheduler.status()["queued"] == 2)
        release_first.set()
        try:
            assert second_entered.wait(3)
            newcomer = pool.submit(read, "new-c", "c", Lock())
            wait_until(lambda: scheduler.status()["queued"] == 2)
        finally:
            release_second.set()
        for result in (initial, waiting, second, newcomer):
            result.result(5)
    assert order == ["b", "a-next", "new-c"]


def test_only_overlapping_identical_previews_share_a_result_and_failures_remain_retryable():
    scheduler = DraftReadScheduler(wait_timeout=5)
    started, release = Event(), Event()
    calls = []

    def read():
        calls.append("read")
        started.set()
        assert release.wait(5)
        return {"previewId": "fresh"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(scheduler.singleflight, ("same-source-revision-values",), read)
        assert started.wait(3)
        follower = pool.submit(scheduler.singleflight, ("same-source-revision-values",), read)
        wait_until(lambda: scheduler.status()["followers"] == 1)
        release.set()
        assert leader.result(3) == ({"previewId": "fresh"}, False)
        assert follower.result(3) == ({"previewId": "fresh"}, True)
    assert calls == ["read"]
    assert scheduler.singleflight(("same-source-revision-values",), read)[1] is False
    assert calls == ["read", "read"]  # A later check must make a fresh read.
    with pytest.raises(RuntimeError, match="failed"):
        scheduler.singleflight(("failed",), lambda: (_ for _ in ()).throw(RuntimeError("failed")))
    assert scheduler.singleflight(("failed",), lambda: "retry") == ("retry", False)


def test_duplicate_waiters_are_bounded_and_receive_the_leader_failure():
    scheduler = DraftReadScheduler(max_followers=1, wait_timeout=5)
    started, release = Event(), Event()

    def read():
        started.set()
        assert release.wait(5)
        raise ValueError("read rejected")

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(scheduler.singleflight, ("source",), read)
        assert started.wait(3)
        follower = pool.submit(scheduler.singleflight, ("source",), read)
        wait_until(lambda: scheduler.status()["followers"] == 1)
        try:
            with pytest.raises(DraftReadBusy, match="follower limit"):
                scheduler.singleflight(("source",), read)
        finally:
            release.set()
        with pytest.raises(ValueError, match="read rejected"):
            leader.result(3)
        with pytest.raises(SharedDraftReadFailure) as failure:
            follower.result(3)
        assert isinstance(failure.value.cause, ValueError)
    assert scheduler.status() == {"active": 0, "queued": 0, "flights": 0, "followers": 0}
