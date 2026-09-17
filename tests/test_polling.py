import asyncio
import time
import pytest
from common.local_store import Store
from common.polling import PollingGuard, PollOwnershipError, STATE_KEY


def test_single_poll_owner(tmp_path):
    a = PollingGuard(Store(tmp_path / "a.db"), "fixture-unique:secret")
    b = PollingGuard(Store(tmp_path / "b.db"), "fixture-unique:different-secret")
    try:
        a.acquire()
        with pytest.raises(PollOwnershipError):
            b.acquire()
    finally:
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_failed_request_preserves_restart_drain(tmp_path):
    store = Store(tmp_path / "a.db")
    guard = PollingGuard(store, "fixture-recovery:secret")
    try:
        with pytest.raises(RuntimeError):
            async with guard.request():
                assert store.get(STATE_KEY)["phase"] == "in_flight"
                raise RuntimeError("connection lost")
        assert store.get(STATE_KEY)["phase"] == "uncertain"
        assert store.get(STATE_KEY)["not_before"] > time.time() + 50
        guard.startup()
        assert store.get(STATE_KEY)["not_before"] > time.time() + 50
    finally:
        guard.close()


@pytest.mark.asyncio
async def test_completed_poll_clears_drain(tmp_path):
    store = Store(tmp_path / "a.db")
    guard = PollingGuard(store, "fixture-complete:secret")
    try:
        async with guard.request():
            await asyncio.sleep(0)
        assert store.get(STATE_KEY)["not_before"] == 0
        assert store.get(STATE_KEY)["phase"] == "completed"
    finally:
        guard.close()


def test_clean_start_has_no_artificial_delay(tmp_path):
    store = Store(tmp_path / "clean.db")
    guard = PollingGuard(store, "fixture-clean-start:secret")
    try:
        guard.startup()
        assert store.get(STATE_KEY)["not_before"] == 0
        assert store.get(STATE_KEY)["phase"] == "ready"
    finally:
        guard.close()
