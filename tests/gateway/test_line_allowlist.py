"""Tests for the LINE source allowlist (user / group / room filtering)."""
from gateway.platforms.line import is_allowed


def test_user_in_allowlist():
    cfg = {"users": ["U1", "U2"], "groups": [], "rooms": []}
    event = {"source": {"type": "user", "userId": "U1"}}
    assert is_allowed(event, cfg) is True


def test_user_not_in_allowlist():
    cfg = {"users": ["U1"], "groups": [], "rooms": []}
    event = {"source": {"type": "user", "userId": "U999"}}
    assert is_allowed(event, cfg) is False


def test_group_in_allowlist():
    cfg = {"users": [], "groups": ["Cabc"], "rooms": []}
    event = {"source": {"type": "group", "groupId": "Cabc", "userId": "U1"}}
    assert is_allowed(event, cfg) is True


def test_room_in_allowlist():
    cfg = {"users": [], "groups": [], "rooms": ["Rxyz"]}
    event = {"source": {"type": "room", "roomId": "Rxyz", "userId": "U1"}}
    assert is_allowed(event, cfg) is True


def test_unknown_source_type_denied():
    cfg = {"users": [], "groups": [], "rooms": []}
    event = {"source": {"type": "weird", "id": "?"}}
    assert is_allowed(event, cfg) is False


def test_empty_allowlists_deny_all():
    cfg = {"users": [], "groups": [], "rooms": []}
    for src in [{"type": "user", "userId": "U1"},
                {"type": "group", "groupId": "C1", "userId": "U1"},
                {"type": "room", "roomId": "R1", "userId": "U1"}]:
        assert is_allowed({"source": src}, cfg) is False


def test_source_type_present_but_id_missing_denied():
    cfg = {"users": ["U1"], "groups": ["C1"], "rooms": ["R1"]}
    assert is_allowed({"source": {"type": "user"}}, cfg) is False
    assert is_allowed({"source": {"type": "group"}}, cfg) is False
    assert is_allowed({"source": {"type": "room"}}, cfg) is False


def test_missing_source_key_denied():
    cfg = {"users": ["U1"], "groups": [], "rooms": []}
    assert is_allowed({}, cfg) is False


def test_allow_all_users_env_bypasses_allowlist(monkeypatch):
    """LINE_ALLOW_ALL_USERS=true makes is_allowed return True for any source."""
    monkeypatch.setenv("LINE_ALLOW_ALL_USERS", "true")
    cfg = {"users": [], "groups": [], "rooms": []}
    # Both an empty allowlist AND an unknown user → still allowed.
    event = {"source": {"type": "user", "userId": "U_NOT_LISTED"}}
    assert is_allowed(event, cfg) is True


def test_allow_all_users_env_false_does_not_bypass(monkeypatch):
    """When LINE_ALLOW_ALL_USERS is unset/false, normal allowlist behavior applies."""
    monkeypatch.delenv("LINE_ALLOW_ALL_USERS", raising=False)
    cfg = {"users": [], "groups": [], "rooms": []}
    event = {"source": {"type": "user", "userId": "U_NOT_LISTED"}}
    assert is_allowed(event, cfg) is False
