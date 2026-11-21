import observer


def test_gift_timing_ignores_fast_stages(monkeypatch):
    observer._GIFT_TIMING_LAST_LOG.clear()
    monkeypatch.setattr(observer.time, "monotonic", lambda: 100.0)

    assert observer._log_gift_timing("creator", "database", 0.49) is False
    assert observer._GIFT_TIMING_LAST_LOG == {}


def test_gift_timing_throttles_repeated_host_stage_logs(monkeypatch):
    observer._GIFT_TIMING_LAST_LOG.clear()
    now = [100.0]
    calls = []
    monkeypatch.setattr(observer.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        observer._GIFT_TIMING_LOG,
        "info",
        lambda *args: calls.append(args),
    )

    assert observer._log_gift_timing("creator", "database", 0.8) is True
    assert observer._log_gift_timing("creator", "database", 1.2) is False
    assert observer._log_gift_timing("creator", "role_callback", 0.8) is True

    now[0] += observer._GIFT_TIMING_COOLDOWN_SECONDS + 0.1
    assert observer._log_gift_timing("creator", "database", 0.9) is True
    assert len(calls) == 3
