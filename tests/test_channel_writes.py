import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")

REALM_URL = "https://realm.example.test"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    core.set_admin_writes_enabled(True)
    request.addfinalizer(lambda: core.set_admin_writes_enabled(False))
    return client


def server(feature_level: int = 500) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "realm_url": REALM_URL,
        "zulip_feature_level": feature_level,
    }


def principal() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "user_id": 1,
        "is_owner": True,
        "is_admin": True,
        "is_bot": False,
        "is_active": True,
    }


def streams(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "streams": items}


def groups() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "user_groups": [{"id": 4, "name": "role:members", "is_system_group": True}],
    }


def users() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "members": [{
            "user_id": 7,
            "full_name": "Synthetic User",
            "email": "user@example.test",
            "is_active": True,
        }],
    }


def test_create_channel_dry_run_resolves_destination_users_and_groups(
    client: Mock,
) -> None:
    core.set_admin_writes_enabled(False)
    client.call_endpoint.side_effect = [
        server(), principal(), streams([]), groups(), users(),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=["user@example.test"],
        privacy="private",
        permissions={"can_send_message_group": "role:members"},
        description="Course discussion",
        settings={
            "history_public_to_subscribers": True,
            "message_retention_days": "unlimited",
            "is_default_stream": False,
        },
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "name": "course",
        "description": "Course discussion",
        "subscribers": [7],
        "invite_only": True,
        "is_web_public": False,
        "history_public_to_subscribers": True,
        "message_retention_days": "unlimited",
        "is_default_stream": False,
        "can_send_message_group": 4,
    }
    assert result["resolved_mappings"]["subscribers"] == [
        {"semantic": "user@example.test", "resolved": 7},
    ]
    assert result["resolved_mappings"]["can_send_message_group"] == {
        "semantic": "role:members",
        "resolved": 4,
    }
    assert all(
        item.kwargs["method"] != "POST"
        for item in client.call_endpoint.call_args_list
    )


def test_create_channel_requires_matching_explicit_realm(client: Mock) -> None:
    client.call_endpoint.side_effect = [server()]

    result = mcp_module.create_channel(
        realm_url="https://other.example.test",
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "DESTINATION_REALM_MISMATCH"
    client.call_endpoint.assert_called_once()


def test_create_channel_requires_feature_417(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(416), principal()]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
    ).structured_content

    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "UNSUPPORTED_FEATURE"


def test_create_channel_rejects_private_default(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal()]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="private",
        permissions={},
        settings={"is_default_stream": True},
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_DEFAULT_CHANNEL"


def test_create_channel_is_idempotent_by_name(client: Mock) -> None:
    existing = {
        "stream_id": 12,
        "name": "course",
        "description": "Course discussion",
        "invite_only": False,
        "is_web_public": False,
        "is_default": False,
        "can_send_message_group": 4,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([existing]), groups(), users(),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={"can_send_message_group": "role:members"},
        description="Course discussion",
        settings={"is_default_stream": False},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == existing
    assert "already exists" in result["warnings"][0]


def test_create_channel_ignores_announce_in_existing_state(client: Mock) -> None:
    existing = {
        "stream_id": 12,
        "name": "course",
        "description": "Course discussion",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([existing]), groups(), users(),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
        description="Course discussion",
        settings={"announce": True},
    ).structured_content

    assert result["status"] == "ok"


def test_create_channel_reports_missing_existing_subscriber(client: Mock) -> None:
    existing = {
        "stream_id": 12,
        "name": "course",
        "description": "",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([existing]), groups(), users(),
        {"result": "success", "msg": "", "subscribers": []},
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=["user@example.test"],
        privacy="public",
        permissions={},
    ).structured_content

    assert result["status"] == "partial"
    assert "missing requested subscribers" in result["warnings"][1]


def test_create_channel_dry_run_reports_missing_existing_subscriber(
    client: Mock,
) -> None:
    existing = {
        "stream_id": 12,
        "name": "course",
        "description": "",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([existing]), groups(), users(),
        {"result": "success", "msg": "", "subscribers": []},
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=["user@example.test"],
        privacy="public",
        permissions={},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert "missing requested subscribers" in result["warnings"][1]


def test_create_channel_conflicts_with_different_existing_channel(client: Mock) -> None:
    existing = {
        "stream_id": 12,
        "name": "course",
        "description": "Different",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([existing]), groups(), users(),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
        description="Desired",
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "CHANNEL_ALREADY_EXISTS"


def test_create_channel_applies_and_reads_back(client: Mock) -> None:
    created = {
        "stream_id": 12,
        "name": "course",
        "description": "Course discussion",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([]), groups(), users(),
        {"result": "success", "msg": "", "stream_id": 12},
        streams([created]),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
        description="Course discussion",
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == created
    assert client.call_endpoint.call_args_list[5] == call(
        url="/channels/create",
        method="POST",
        request={
            "name": "course",
            "description": "Course discussion",
            "subscribers": [],
            "invite_only": False,
            "is_web_public": False,
        },
    )


def test_create_channel_reports_mismatched_readback_as_partial(client: Mock) -> None:
    created = {
        "stream_id": 12,
        "name": "course",
        "description": "Unexpected",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([]), groups(), users(),
        {"result": "success", "msg": "", "stream_id": 12},
        streams([created]),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=[],
        privacy="public",
        permissions={},
        description="Course discussion",
    ).structured_content

    assert result["status"] == "partial"
    assert result["warnings"] == [
        "Channel readback did not match requested values: description",
    ]


def test_create_channel_reports_missing_subscriber_readback(client: Mock) -> None:
    created = {
        "stream_id": 12,
        "name": "course",
        "description": "",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([]), groups(), users(),
        {"result": "success", "msg": "", "stream_id": 12},
        streams([created]),
        {"result": "success", "msg": "", "subscribers": []},
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=["user@example.test"],
        privacy="public",
        permissions={},
    ).structured_content

    assert result["status"] == "partial"
    assert "missing requested subscribers" in result["warnings"][0]


def test_create_channel_rejects_numeric_cross_realm_references(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server(), principal(), streams([]), groups(), users(),
    ]

    result = mcp_module.create_channel(
        realm_url=REALM_URL,
        name="course",
        subscribers=["user@example.test"],
        privacy="public",
        permissions={"can_send_message_group": 4},
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_update_channel_dry_run_uses_expected_and_optimistic_permissions(
    client: Mock,
) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "description": "old",
        "invite_only": False,
        "is_web_public": False,
        "can_send_message_group": 4,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
    ]

    result = mcp_module.update_channel_configuration(
        realm_url=REALM_URL,
        channel="course",
        changes={
            "description": "new",
            "can_send_message_group": {
                "direct_members": ["user@example.test"],
                "direct_subgroups": [],
            },
        },
        expected={"description": "old"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/streams/12"
    assert result["request"] == {
        "description": "new",
        "can_send_message_group": {
            "new": {"direct_members": [7], "direct_subgroups": []},
            "old": 4,
        },
    }
    assert result["resolved_mappings"]["channel"] == {
        "semantic": "course", "resolved": 12,
    }


def test_update_channel_expected_conflict_makes_no_patch(client: Mock) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "description": "changed",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
    ]

    result = mcp_module.update_channel_configuration(
        realm_url=REALM_URL,
        channel="course",
        changes={"description": "new"},
        expected={"description": "old"},
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_update_channel_rejects_null_privacy(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal()]

    result = mcp_module.update_channel_configuration(
        realm_url=REALM_URL,
        channel="course",
        changes={"privacy": None},
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "NULL_WRITE_VALUE"
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_update_channel_applies_and_reads_back(client: Mock) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "description": "old",
        "invite_only": False,
        "is_web_public": False,
        "is_default": False,
    }
    updated = {**current, "description": "new", "is_default": True}
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
        {"result": "success", "msg": ""},
        streams([updated]),
    ]

    result = mcp_module.update_channel_configuration(
        realm_url=REALM_URL,
        channel="course",
        changes={"description": "new", "is_default_stream": True},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {
        "description": "new",
        "is_default_stream": True,
    }
    assert client.call_endpoint.call_args_list[5] == call(
        url="/streams/12",
        method="PATCH",
        request={"description": "new", "is_default_stream": True},
    )


def test_update_channel_readback_failure_is_partial(client: Mock) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "description": "old",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
        {"result": "success", "msg": ""},
        {"result": "error", "msg": "readback unavailable", "code": "BAD_GATEWAY"},
    ]

    result = mcp_module.update_channel_configuration(
        realm_url=REALM_URL,
        channel="course",
        changes={"description": "new"},
    ).structured_content

    assert result["status"] == "partial"
    assert result["response"] == {}
    assert result["error"]["code"] == "BAD_GATEWAY"


def test_set_default_channel_is_idempotent(client: Mock) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "invite_only": False,
        "is_web_public": False,
        "is_default": True,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
    ]

    result = mcp_module.set_default_channel(
        realm_url=REALM_URL,
        channel="course",
        is_default=True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"] == {}
    assert "already had" in result["warnings"][0]


def test_set_default_channel_rejects_private_channel(client: Mock) -> None:
    current = {
        "stream_id": 12,
        "name": "course",
        "invite_only": True,
        "is_web_public": False,
        "is_default": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([current]), groups(), users(),
    ]

    result = mcp_module.set_default_channel(
        realm_url=REALM_URL,
        channel="course",
        is_default=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_DEFAULT_CHANNEL"


def test_subscribe_users_dry_run_only_includes_missing_users(client: Mock) -> None:
    channel = {
        "stream_id": 12,
        "name": "course",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([channel]), groups(), users(),
        {"result": "success", "msg": "", "subscribers": []},
    ]

    result = mcp_module.subscribe_users_to_channel(
        realm_url=REALM_URL,
        channel="course",
        users=["user@example.test"],
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "subscriptions": [{"name": "course"}],
        "principals": [7],
        "authorization_errors_fatal": True,
    }
    assert result["changed_fields"] == ["user@example.test"]


def test_subscribe_users_applies_and_reads_back(client: Mock) -> None:
    channel = {
        "stream_id": 12,
        "name": "course",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([channel]), groups(), users(),
        {"result": "success", "msg": "", "subscribers": []},
        {
            "result": "success",
            "msg": "",
            "subscribed": {"7": ["course"]},
            "already_subscribed": {},
        },
        {"result": "success", "msg": "", "subscribers": [7]},
    ]

    result = mcp_module.subscribe_users_to_channel(
        realm_url=REALM_URL,
        channel="course",
        users=["Synthetic User"],
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"subscriber_ids": [7]}
    assert client.call_endpoint.call_args_list[6] == call(
        url="/users/me/subscriptions",
        method="POST",
        request={
            "subscriptions": [{"name": "course"}],
            "principals": [7],
            "authorization_errors_fatal": True,
        },
    )


def test_subscribe_users_readback_failure_is_partial(client: Mock) -> None:
    channel = {
        "stream_id": 12,
        "name": "course",
        "invite_only": False,
        "is_web_public": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), streams([channel]), groups(), users(),
        {"result": "success", "msg": "", "subscribers": []},
        {"result": "success", "msg": "", "subscribed": {"7": ["course"]}},
        {"result": "error", "msg": "readback unavailable", "code": "BAD_GATEWAY"},
    ]

    result = mcp_module.subscribe_users_to_channel(
        realm_url=REALM_URL,
        channel="course",
        users=["Synthetic User"],
    ).structured_content

    assert result["status"] == "partial"
    assert result["response"] == {"subscribed": {"7": ["course"]}}
    assert result["error"]["code"] == "BAD_GATEWAY"
