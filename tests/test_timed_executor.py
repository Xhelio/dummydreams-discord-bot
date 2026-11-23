from timed_executor import call_with_timing


def test_call_with_timing_returns_result_and_nonnegative_worker_duration():
    result, elapsed = call_with_timing(lambda left, right: left + right, 2, 3)

    assert result == 5
    assert elapsed >= 0


def test_call_with_timing_passes_keyword_arguments():
    def combine(value, *, suffix):
        return f"{value}{suffix}"

    result, elapsed = call_with_timing(combine, "gift", suffix="-event")

    assert result == "gift-event"
    assert elapsed >= 0
