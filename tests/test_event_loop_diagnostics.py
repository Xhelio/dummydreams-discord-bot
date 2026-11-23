# Regression coverage: TLS handshakes must never observe a runtime debug-mode toggle.
from event_loop_diagnostics import SlowCallbackDiagnostics


class FakeHandle:
    def __init__(self, callback):
        self.callback = callback

    def fire(self):
        self.callback()


class FakeLoop:
    def __init__(self, debug=False, threshold=0.5):
        self.debug = debug
        self.slow_callback_duration = threshold
        self.handles = []
        self.set_debug_calls = []

    def get_debug(self):
        return self.debug

    def set_debug(self, value):
        self.set_debug_calls.append(value)
        self.debug = value

    def call_later(self, delay, callback):
        self.delay = delay
        handle = FakeHandle(callback)
        self.handles.append(handle)
        return handle


def test_diagnostics_do_not_toggle_debug_when_disabled():
    loop = FakeLoop(debug=False, threshold=0.75)
    diagnostics = SlowCallbackDiagnostics(duration_seconds=120, threshold_seconds=0.2)

    assert diagnostics.activate(loop) is False
    assert loop.debug is False
    assert loop.slow_callback_duration == 0.75
    assert loop.handles == []
    assert loop.set_debug_calls == []


def test_diagnostics_tighten_and_restore_threshold_when_debug_is_enabled():
    loop = FakeLoop(debug=True, threshold=0.75)
    diagnostics = SlowCallbackDiagnostics(duration_seconds=120, threshold_seconds=0.2)

    assert diagnostics.activate(loop) is True
    assert loop.debug is True
    assert loop.slow_callback_duration == 0.2
    assert loop.delay == 120
    assert loop.set_debug_calls == []

    loop.handles[0].fire()

    assert loop.debug is True
    assert loop.slow_callback_duration == 0.75
    assert diagnostics._restore_handle is None
    assert loop.set_debug_calls == []


def test_diagnostics_never_raise_an_existing_higher_threshold():
    loop = FakeLoop(debug=True, threshold=0.1)
    diagnostics = SlowCallbackDiagnostics(threshold_seconds=0.25)

    assert diagnostics.activate(loop) is True
    assert loop.slow_callback_duration == 0.1
    assert loop.set_debug_calls == []


def test_diagnostics_do_not_schedule_duplicate_restore_callbacks():
    loop = FakeLoop(debug=True)
    diagnostics = SlowCallbackDiagnostics()

    assert diagnostics.activate(loop) is True
    assert diagnostics.activate(loop) is False
    assert len(loop.handles) == 1
    assert loop.set_debug_calls == []
