import hashlib
import importlib
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from zulipmcp import core
from zulipmcp.configuration import (
    APIError, ConfigurationQueueSnapshot, ZulipAPIError,
)

mcp_module = importlib.import_module("zulipmcp.mcp")

REALM_URL = "https://realm.example.test"
CONTENT = "This is the exact message body."
DIGEST = hashlib.sha256(CONTENT.encode()).hexdigest()


def message() -> dict[str, object]:
    return {
        "id": 42,
        "type": "stream",
        "display_recipient": "moderated",
        "subject": "topic",
        "sender_email": "sender@example.test",
        "sender_full_name": "Sender",
        "sender_id": 7,
        "timestamp": 1700000000,
        "content": CONTENT,
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    server = {
        "realm_url": REALM_URL,
        "zulip_feature_level": 500,
        "server_report_message_types": ["spam", "other"],
    }
    monkeypatch.setattr(
        core, "_admin_destination",
        lambda endpoint, realm_url, dry_run: (server, {"is_admin": True}, None),
    )
    monkeypatch.setattr(
        core, "_realm_destination",
        lambda endpoint, realm_url, dry_run: (server, None),
    )
    monkeypatch.setattr(core, "_stream_write_error", lambda stream: None)

    @contextmanager
    def report_snapshot() -> object:
        yield ConfigurationQueueSnapshot(data={
            "realm_moderation_request_channel_id": 12,
            "server_report_message_types": [
                {"key": "spam", "name": "Spam"},
                {"key": "other", "name": "Other"},
            ],
        })

    monkeypatch.setattr(core, "configuration_queue_snapshot", report_snapshot)
    core.set_user_content_writes_enabled(True)
    yield
    core.set_user_content_writes_enabled(False)


def test_delete_message_dry_run_returns_verified_impact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["current"]["content_sha256"] == DIGEST
    assert result["current"]["content_preview"] == CONTENT
    assert any("DELETE MESSAGE 42" in warning for warning in result["warnings"])


def test_delete_message_rejects_stale_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="someone-else@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert "sender_email" in result["error"]["message"]


def test_delete_message_requires_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_request", mutation)

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        confirmation="delete it",
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "CONFIRMATION_REQUIRED"
    mutation.assert_not_called()


def test_delete_message_obeys_channel_write_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())
    monkeypatch.setattr(
        core, "_stream_write_error",
        lambda stream: {"result": "error", "msg": "Stream write access denied"},
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_request", mutation)

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        confirmation="DELETE MESSAGE 42",
    ).structured_content

    assert result["status"] == "forbidden"
    assert result["error"]["code"] == "CHANNEL_WRITE_DENIED"
    mutation.assert_not_called()


def test_delete_message_applies_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_get_moderation_message", Mock(side_effect=[message(), None]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_request", mutation)

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        confirmation="DELETE MESSAGE 42",
    ).structured_content

    assert result["status"] == "ok"
    mutation.assert_called_once_with("/messages/42", "DELETE", request={})


def test_delete_message_readback_failure_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_get_moderation_message",
        Mock(side_effect=[
            message(),
            ZulipAPIError(APIError("readback failed", "BAD_GATEWAY", 502)),
        ]),
    )
    monkeypatch.setattr(core, "configuration_request", Mock(return_value={}))

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        confirmation="DELETE MESSAGE 42",
    ).structured_content

    assert result["status"] == "partial"
    assert result["error"]["code"] == "BAD_GATEWAY"


def test_delete_message_obeys_user_content_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())
    core.set_user_content_writes_enabled(False)

    result = mcp_module.delete_message_for_moderation(
        realm_url=REALM_URL,
        message_id=42,
        expected_sender="sender@example.test",
        expected_timestamp=1700000000,
        expected_content_sha256=DIGEST,
        expected_channel="moderated",
        expected_topic="topic",
        confirmation="DELETE MESSAGE 42",
    ).structured_content

    assert result["status"] == "disabled"
    assert result["error"]["code"] == "USER_CONTENT_WRITES_DISABLED"


def test_report_message_validates_type_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())

    invalid_type = mcp_module.report_message(
        realm_url=REALM_URL, message_id=42, report_type="abuse",
        description="bad", expected_sender="sender@example.test",
        expected_content_sha256=DIGEST, dry_run=True,
    ).structured_content
    assert invalid_type["status"] == "error"
    assert invalid_type["error"]["code"] == "INVALID_REPORT_TYPE"

    missing_description = mcp_module.report_message(
        realm_url=REALM_URL, message_id=42, report_type="other",
        description="", expected_sender="sender@example.test",
        expected_content_sha256=DIGEST, dry_run=True,
    ).structured_content
    assert missing_description["status"] == "error"
    assert missing_description["error"]["code"] == "DESCRIPTION_REQUIRED"


def test_report_message_rejects_unconfigured_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @contextmanager
    def unconfigured_snapshot() -> object:
        yield ConfigurationQueueSnapshot(data={
            "realm_moderation_request_channel_id": -1,
            "server_report_message_types": [
                {"key": "spam", "name": "Spam"},
            ],
        })

    monkeypatch.setattr(core, "configuration_queue_snapshot", unconfigured_snapshot)

    result = mcp_module.report_message(
        realm_url=REALM_URL, message_id=42, report_type="spam",
        description="bad", expected_sender="sender@example.test",
        expected_content_sha256=DIGEST, dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "MODERATION_DESTINATION_UNCONFIGURED"


def test_report_message_is_explicit_visible_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_request", mutation)

    result = mcp_module.report_message(
        realm_url=REALM_URL,
        message_id=42,
        report_type="spam",
        description="Unsolicited advertising",
        expected_sender="sender@example.test",
        expected_content_sha256=DIGEST,
        confirmation="REPORT MESSAGE 42",
    ).structured_content

    assert result["status"] == "ok"
    mutation.assert_called_once_with(
        "/messages/42/report", "POST",
        request={
            "report_type": "spam", "description": "Unsolicited advertising",
        },
    )


def test_report_message_propagates_server_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_get_moderation_message", lambda message_id: message())
    monkeypatch.setattr(
        core,
        "configuration_request",
        Mock(side_effect=ZulipAPIError(APIError("denied", "FORBIDDEN", 403))),
    )

    result = mcp_module.report_message(
        realm_url=REALM_URL,
        message_id=42,
        report_type="spam",
        description="Unsolicited advertising",
        expected_sender="sender@example.test",
        expected_content_sha256=DIGEST,
        confirmation="REPORT MESSAGE 42",
    ).structured_content

    assert result["status"] == "forbidden"


def test_moderation_audit_resolves_destination_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "get_server_settings", lambda: {
        "zulip_feature_level": 500,
        "server_report_message_types": ["spam", "other"],
    })
    monkeypatch.setattr(core, "get_current_user", lambda: {
        "user_id": 1, "email": "admin@example.test", "is_admin": True,
    })
    monkeypatch.setattr(core, "get_user_groups_configuration", lambda: {
        "user_groups": [{"id": 4, "name": "role:administrators"}],
    })
    monkeypatch.setattr(core, "get_streams_configuration", lambda **kwargs: {
        "streams": [{
            "stream_id": 12, "name": "moderation", "is_archived": False,
            "can_delete_any_message_group": 4,
        }],
    })

    @contextmanager
    def snapshot() -> object:
        yield ConfigurationQueueSnapshot(data={
            "realm_moderation_request_channel_id": 12,
            "realm_message_content_delete_limit_seconds": 300,
            "realm_can_delete_any_message_group": 4,
        })

    monkeypatch.setattr(core, "configuration_queue_snapshot", snapshot)

    result = mcp_module.get_moderation_configuration().structured_content

    assert result["status"] == "ok"
    data = result["data"]
    assert data["reporting"]["moderation_destination"]["name"] == "moderation"
    assert data["resolved_realm_permission_groups"][
        "realm_can_delete_any_message_group"
    ]["group_name"] == "role:administrators"
    assert data["known_gaps"]
