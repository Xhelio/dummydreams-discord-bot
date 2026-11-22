"""Create an asyncio default executor with workers started before the loop."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


def create_warmed_executor(max_workers: int, thread_name_prefix: str = "db"):
    """Start all executor workers eagerly so later submissions do not start threads on the event loop."""
    worker_count = int(max_workers)
    if worker_count < 1:
        raise ValueError("max_workers must be at least 1")

    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=thread_name_prefix,
    )
    barrier = Barrier(worker_count)
    try:
        futures = [executor.submit(barrier.wait) for _ in range(worker_count)]
        for future in futures:
            future.result()
    except Exception:
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    return executor
