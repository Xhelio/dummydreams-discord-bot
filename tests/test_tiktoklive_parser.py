import asyncio
import threading
from types import SimpleNamespace

import tiktoklive_parser


class FakeLogger:
    def __init__(self):
        self.records = []

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.records.append(("error", args, kwargs))

    def debug(self, *args, **kwargs):
        self.records.append(("debug", args, kwargs))


def make_client():
    client = object.__new__(tiktoklive_parser.NonBlockingTikTokLiveClient)
    client._logger = FakeLogger()
    client.parse_error_ignorelist = []
    client.ignore_broken_payload = False

    async def handle_custom_event(message, event):
        return None

    client.handle_custom_event = handle_custom_event
    return client


def test_protobuf_decode_runs_outside_event_loop_thread(monkeypatch):
    loop_thread = threading.get_ident()
    decoded_threads = []

    class FakeProtoEvent:
        def parse(self, payload):
            decoded_threads.append(threading.get_ident())
            return {"payload": payload}

    monkeypatch.setattr(
        tiktoklive_parser, "EVENT_MAPPINGS", {"FakeMessage": FakeProtoEvent}
    )
    response = SimpleNamespace()
    message = SimpleNamespace(method="FakeMessage", payload=b"protobuf")

    client = make_client()
    events = asyncio.run(
        client._parse_webcast_response_message(response, message)
    )

    assert decoded_threads
    assert decoded_threads[0] != loop_thread
    assert len(events) == 2
    assert events[1] == {"payload": b"protobuf"}


def test_unknown_message_keeps_existing_fallback(monkeypatch):
    monkeypatch.setattr(tiktoklive_parser, "EVENT_MAPPINGS", {})
    response = SimpleNamespace()
    message = SimpleNamespace(method="FutureMessage", payload=b"payload")

    events = asyncio.run(
        make_client()._parse_webcast_response_message(response, message)
    )

    assert len(events) == 2
    assert isinstance(events[0], tiktoklive_parser.WebsocketResponseEvent)
    assert isinstance(events[1], tiktoklive_parser.UnknownEvent)
