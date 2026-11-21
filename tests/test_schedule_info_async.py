import asyncio
import threading

import utils


def test_get_schedule_info_async_offloads_blocking_lookup(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    worker_thread_ids = []
    loop_thread_id = threading.get_ident()

    def blocking_lookup(username):
        worker_thread_ids.append(threading.get_ident())
        started.set()
        assert release.wait(timeout=2)
        return f"host:{username}", f"channel:{username}"

    monkeypatch.setattr(utils, "get_schedule_info", blocking_lookup)

    async def run_test():
        task = asyncio.create_task(utils.get_schedule_info_async("example"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # The event loop remains responsive while the synchronous lookup waits.
            heartbeat = False
            await asyncio.sleep(0)
            heartbeat = True
            assert heartbeat
        finally:
            release.set()

        assert await task == ("host:example", "channel:example")
        assert worker_thread_ids
        assert worker_thread_ids[0] != loop_thread_id

    asyncio.run(run_test())
