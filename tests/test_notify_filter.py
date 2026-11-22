import logging

from log_setup import _NotifyFilter


def _record(name, message, level=logging.WARNING):
    return logging.LogRecord(name, level, __file__, 1, message, (), None)


def test_asyncio_slow_callback_warnings_stay_out_of_db_and_alerts():
    filt = _NotifyFilter()
    record = _record("asyncio", "Executing <Task pending name='Task-1'> took 0.5 seconds")

    assert filt.filter(record) is False


def test_other_asyncio_warnings_are_still_notified():
    filt = _NotifyFilter()
    record = _record("asyncio", "Task exception was never retrieved")

    assert filt.filter(record) is True


def test_application_warnings_are_still_notified():
    filt = _NotifyFilter()
    record = _record("bot.observer", "Observer connection failed")

    assert filt.filter(record) is True
