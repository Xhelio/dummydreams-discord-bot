"""Keep TikTok's synchronous protobuf decoding off the Discord event loop.

TikTokLive 7.0.1 exposes an async parser method, but its protobuf decode is
synchronous and recursively parses nested betterproto2 messages. On busy rooms
that work can monopolize the event loop long enough to miss Discord ACKs.
"""

import asyncio
import time

from TikTokLive.client.client import TikTokLiveClient as _TikTokLiveClient
from TikTokLive.events.custom_events import UnknownEvent, WebsocketResponseEvent
from TikTokLive.events.proto_events import EVENT_MAPPINGS


def _parse_proto_event(event_type, payload):
    """Construct and decode an event away from the asyncio event-loop thread."""
    return event_type().parse(payload)


class NonBlockingTikTokLiveClient(_TikTokLiveClient):
    """TikTokLive client that decodes protobuf payloads in a worker thread."""

    async def _parse_webcast_response_message(
        self, webcast_response, webcast_response_message
    ):
        if webcast_response_message is None:
            self._logger.warning(
                "Received a null ProtoMessageFetchResultMessage from the Webcast server."
            )
            return []

        event_type = EVENT_MAPPINGS.get(webcast_response_message.method)
        response_event = WebsocketResponseEvent(raw=webcast_response)
        if event_type is None:
            return [response_event, UnknownEvent(raw=webcast_response)]

        payload = webcast_response_message.payload or b""
        parse_started = time.perf_counter()
        try:
            # The protobuf parser is CPU-heavy Python code. Moving it off the
            # loop keeps Discord interactions and other observers schedulable.
            proto_event = await asyncio.to_thread(_parse_proto_event, event_type, payload)
        except Exception as exc:
            error_text = str(exc)
            is_known_schema_bug = any(
                token in error_text for token in self.parse_error_ignorelist
            )
            if is_known_schema_bug:
                self._logger.debug(
                    "Skipped %s payload (known upstream schema mismatch): %s",
                    webcast_response_message.method,
                    type(exc).__name__,
                )
            elif not self.ignore_broken_payload:
                preview = repr(payload[:32])
                if len(payload) > 32:
                    preview += f"...(+{len(payload) - 32} more bytes)"
                self._logger.error(
                    "Failed to parse %s payload (%d bytes); raw=%s",
                    webcast_response_message.method,
                    len(payload),
                    preview,
                    exc_info=True,
                )
            return [response_event]

        parse_elapsed = time.perf_counter() - parse_started
        if parse_elapsed >= 0.5:
            self._logger.warning(
                "[TikTok Parser] method=%s payload_bytes=%d elapsed=%.3fs offloaded=true",
                webcast_response_message.method,
                len(payload),
                parse_elapsed,
            )

        parsed_events = [response_event, proto_event]
        custom_event = await self.handle_custom_event(
            webcast_response_message, proto_event
        )
        return [custom_event, *parsed_events] if custom_event else parsed_events
