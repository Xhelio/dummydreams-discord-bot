import threading

import pytest

from executor_warmup import create_warmed_executor


def test_create_warmed_executor_starts_all_workers():
    executor = create_warmed_executor(3, thread_name_prefix="test-db")
    try:
        assert len(executor._threads) == 3
        names = [
            executor.submit(lambda: threading.current_thread().name).result()
            for _ in range(3)
        ]
        assert all(name.startswith("test-db") for name in names)
    finally:
        executor.shutdown(wait=True)


def test_create_warmed_executor_rejects_zero_workers():
    with pytest.raises(ValueError, match="at least 1"):
        create_warmed_executor(0)
