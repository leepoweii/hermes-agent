import base64
import hashlib
import hmac
import json

import pytest

from gateway.platforms.line.webhook import verify_signature, parse_events


def _sign(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_verify_signature_accepts_valid():
    secret = "test_secret"
    body = b'{"events":[]}'
    sig = _sign(secret, body)
    assert verify_signature(body, sig, secret) is True


def test_verify_signature_rejects_invalid():
    body = b'{"events":[]}'
    bad_sig = "not-the-real-signature"
    assert verify_signature(body, bad_sig, "test_secret") is False


def test_verify_signature_rejects_empty():
    assert verify_signature(b"", "", "test_secret") is False


def test_parse_events_returns_empty_for_no_events():
    body = json.dumps({"destination": "U1", "events": []}).encode()
    assert parse_events(body) == []


def test_parse_events_extracts_message_event():
    payload = {
        "destination": "U1",
        "events": [
            {
                "type": "message",
                "replyToken": "rt-1",
                "source": {"type": "user", "userId": "Uabc"},
                "timestamp": 1234567890,
                "message": {"id": "m1", "type": "text", "text": "hi"},
            }
        ],
    }
    body = json.dumps(payload).encode()
    events = parse_events(body)
    assert len(events) == 1
    assert events[0]["type"] == "message"
    assert events[0]["source"]["userId"] == "Uabc"


def test_verify_signature_accepts_valid_empty_body():
    secret = "test_secret"
    body = b""
    sig = _sign(secret, body)
    assert verify_signature(body, sig, secret) is True


def test_parse_events_returns_empty_for_malformed_json():
    assert parse_events(b"not json") == []


def test_parse_events_returns_empty_for_non_dict_payload():
    assert parse_events(b"[1,2,3]") == []
