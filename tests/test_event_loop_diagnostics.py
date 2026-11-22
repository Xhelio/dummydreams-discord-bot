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

    def get_debug(self):
        return self.debug

    def set_debug(self, value):
        self.debug = value

    def call_later(self, delay, callback):
        self.delay = delay
        handle = FakeHandle(callback)
        self.handles.append(handle)
        return handle


def test_diagnostics_enable_and_restore_previous_loop_settings():
    loop = FakeLoop(debug=False, threshold=0.75)
    diagnostics = SlowCallbackDiagnostics(duration_seconds=120, threshold_seconds=0.2)

    assert diagnostics.activate(loop) is True
    assert loop.debug is True
    assert loop.slow_callback_duration == 0.2
    assert loop.delay == 120

    loop.handles[0].fire()

    assert loop.debug is False
    assert loop.slow_callback_duration == 0.75
    assert diagnostics._restore_handle is None


def test_diagnostics_do_not_override_existing_debug_mode():
    loop = FakeLoop(debug=True, threshold=0.4)
    diagnostics = SlowCallbackDiagnostics()

    assert diagnostics.activate(loop) is False
    assert loop.debug is True
    assert loop.slow_callback_duration == 0.4
    assert loop.handles == []


def test_diagnostics_do_not_schedule_duplicate_restore_callbacks():
    loop = FakeLoop()
    diagnostics = SlowCallbackDiagnostics()

    assert diagnostics.activate(loop) is True
    assert diagnostics.activate(loop) is False
    assert len(loop.handles) == 1
