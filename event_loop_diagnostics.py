"""Temporary asyncio slow-callback tracing for diagnosing event-loop stalls."""


class SlowCallbackDiagnostics:
    """Enable asyncio's built-in slow-callback logger for a bounded window."""

    def __init__(self, duration_seconds: float = 180.0, threshold_seconds: float = 0.25):
        self.duration_seconds = max(1.0, float(duration_seconds))
        self.threshold_seconds = max(0.01, float(threshold_seconds))
        self._restore_handle = None

    def activate(self, loop) -> bool:
        """Enable debug tracing once; restore the loop's previous settings later."""
        if loop.get_debug() or self._restore_handle is not None:
            return False

        previous_debug = loop.get_debug()
        previous_threshold = loop.slow_callback_duration
        loop.set_debug(True)
        loop.slow_callback_duration = self.threshold_seconds

        def restore():
            loop.slow_callback_duration = previous_threshold
            loop.set_debug(previous_debug)
            self._restore_handle = None

        self._restore_handle = loop.call_later(self.duration_seconds, restore)
        return True
