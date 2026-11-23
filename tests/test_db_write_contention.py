import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import db


class _FakeConnection:
    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


@pytest.fixture
def isolated_write_path(monkeypatch):
    """Keep contention tests entirely local; never connect to Turso."""
    monkeypatch.setattr(db, "HAS_LIBSQL", False)
    monkeypatch.setattr(db, "TURSO_URL", None)
    monkeypatch.setattr(db, "_DB_WRITE_CONNECTION", None)
    monkeypatch.setattr(db, "_DB_WRITE_LOCK", threading.Lock())
    monkeypatch.setattr(db, "_DB_WRITE_WAITERS", 0)
    monkeypatch.setattr(db, "_DB_WRITE_WAITERS_LOCK", threading.Lock())
    monkeypatch.setattr(db, "get_db_connection", _FakeConnection)


@pytest.mark.parametrize("writer_count", [10, 25, 50])
def test_write_transactions_serialize_under_concurrent_bursts(
    isolated_write_path, writer_count
):
    active_writers = 0
    max_active_writers = 0
    counter_lock = threading.Lock()

    def operation(_connection):
        nonlocal active_writers, max_active_writers
        with counter_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        time.sleep(0.003)
        with counter_lock:
            active_writers -= 1
        return "committed"

    with ThreadPoolExecutor(max_workers=writer_count) as executor:
        results = list(
            executor.map(
                lambda _index: db.write_transaction(operation),
                range(writer_count),
            )
        )

    assert results == ["committed"] * writer_count
    assert max_active_writers == 1
    assert db._DB_WRITE_WAITERS == 0


def test_offloaded_write_contention_does_not_block_asyncio_loop(
    isolated_write_path,
):
    async def run_scenario():
        ticks = 0

        def slow_write(_connection):
            time.sleep(0.04)
            return "committed"

        async def heartbeat():
            nonlocal ticks
            for _ in range(6):
                await asyncio.sleep(0.005)
                ticks += 1

        writes = [
            asyncio.to_thread(db.write_transaction, slow_write)
            for _ in range(10)
        ]
        results = await asyncio.gather(*writes, heartbeat())
        return results[:-1], ticks

    results, ticks = asyncio.run(run_scenario())

    assert results == ["committed"] * 10
    assert ticks == 6
