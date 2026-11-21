import hashlib
import hmac

import pytest

from routes.api import (
    _extract_euler_username,
    _infer_euler_live_state,
    _verify_euler_webhook_signature,
)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"username": "@ExampleUser"}, "exampleuser"),
        ({"data": {"user": {"uniqueId": "NestedUser"}}}, "nesteduser"),
        ({"creator": {"username": "CreatorName"}}, "creatorname"),
        ({"username": "  "}, ""),
        (None, ""),
        (["not", "a", "mapping"], ""),
    ],
)
def test_extract_euler_username(payload, expected):
    assert _extract_euler_username(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"is_live": True}, True),
        ({"isLive": "false"}, False),
        ({"status": "streaming"}, True),
        ({"event": "live_ended"}, False),
        ({"room_status": 2}, True),
        ({"roomStatus": 4}, False),
        ({"status": "unknown"}, None),
        (None, None),
    ],
)
def test_infer_euler_live_state(payload, expected):
    assert _infer_euler_live_state(payload) is expected


def test_webhook_signature_accepts_valid_sha256_signature():
    body = b'{"username":"creator","is_live":true}'
    secret = "test-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert _verify_euler_webhook_signature(body, signature, secret)


def test_webhook_signature_accepts_sha256_prefix():
    body = b"payload"
    secret = "test-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert _verify_euler_webhook_signature(body, f"sha256={signature}", secret)


@pytest.mark.parametrize(
    ("body", "signature", "secret"),
    [
        (b"payload", "", "secret"),
        (b"payload", "abcd", ""),
        (b"different", "not-a-valid-signature", "secret"),
    ],
)
def test_webhook_signature_rejects_invalid_input(body, signature, secret):
    assert not _verify_euler_webhook_signature(body, signature, secret)
