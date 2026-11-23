"""Measure synchronous work separately from executor dispatch overhead."""
import time


def call_with_timing(func, *args, **kwargs):
    """Return (function_result, worker_elapsed_seconds) for a sync callable."""
    started = time.perf_counter()
    result = func(*args, **kwargs)
    return result, time.perf_counter() - started
