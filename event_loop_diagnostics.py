"""Bounded asyncio slow-callback threshold tuning without toggling loop debug.

asyncio's SSLProtocol records handshake timing only when loop debug is enabled
at handshake start. Toggling debug while a TLS handshake is in flight can make
CPython 3.11 subtract None from a float when the handshake completes.
Debug mode must therefore remain stable for the lifetime of the event loop.
"""


class SlowCallbackDiagnostics:
    """Temporarily tighten slow-callback logging when debug mode is already on."""

    def __init__(self, duration_seconds: float = 180.0, threshold_seconds: float = 0.25):
        self.duration_seconds = max(1.0, float(duration_seconds))
        self.threshold_seconds = max(0.01, float(threshold_seconds))
        self._restore_handle = None

    def activate(self, loop) -> bool:
        """Lower the callback threshold only; never change loop debug state."""
        if not loop.get_debug() or self._restore_handle is not None:
            return False

        previous_threshold = loop.slow_callback_duration
        loop.slow_callback_duration = min(
            previous_threshold, self.threshold_seconds
        )

        def restore():
            loop.slow_callback_duration = previous_threshold
            self._restore_handle = None

        self._restore_handle = loop.call_later(self.duration_seconds, restore)
        return True
