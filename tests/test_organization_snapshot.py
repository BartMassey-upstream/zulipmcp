import importlib
from unittest.mock import Mock, call

import pytest
from fastmcp.tools.tool import ToolResult

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


def server_response() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "zulip_version": "12.3",
        "zulip_feature_level": 500,
    }


def principal_response() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "user_id": 1,
        "full_name": "Synthetic Owner",
        "email": "owner@example.test",
        "role": 100,
        "is_owner": True,
        "is_admin": True,
        "is_guest": False,
        "is_bot": False,
        "is_active": True,
    }


def register_response() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "queue_id": "queue-1",
        "realm_name": "Synthetic realm",
        "realm_description": "A test realm",
        "realm_authentication_methods": {"Email": True},
        "realm_invite_required": True,
        "realm_message_retention_days": -1,
        "realm_message_content_edit_limit_seconds": 600,
        "realm_message_content_delete_limit_seconds": 300,
        "realm_move_messages_between_streams_limit_seconds": 3600,
        "realm_move_messages_within_stream_limit_seconds": 7200,
        "realm_message_content_allowed_in_email_notifications": False,
        "realm_enable_spectator_access": True,
        "realm_email_changes_disabled": True,
        "realm_can_create_public_channel_group": 4,
        "realm_can_delete_own_message_group": {
            "direct_members": [],
            "direct_subgroups": [4],
        },
        "server_supported_permission_settings": {
            "realm_can_create_public_channel_group": [1, 2, 3, 4],
        },
        "realm_user_settings_defaults": {
            "enable_stream_desktop_notifications": False,
        },
        "default_streams": [12],
        "default_stream_groups": [],
    }


def test_aggregate_snapshot_is_typed_resolved_and_queue_safe(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        register_response(),
        {"result": "success", "msg": ""},
        {
            "result": "success",
            "msg": "",
            "user_groups": [{"id": 4, "name": "administrators", "members": [1]}],
        },
        {"result": "success", "msg": "", "invites": []},
        {
            "result": "success",
            "msg": "",
            "members": [
                {
                    "user_id": 1,
                    "full_name": "Synthetic Owner",
                    "email": "owner@example.test",
                    "is_active": True,
                    "is_bot": False,
                    "delivery_email": "private@example.test",
                },
                {
                    "user_id": 2,
                    "full_name": "Synthetic Bot",
                    "email": "bot@example.test",
                    "is_active": True,
                    "is_bot": True,
                    "bot_type": 1,
                    "api_key": "bot-private-key",
                },
            ],
        },
        {
            "result": "success",
            "msg": "",
            "streams": [{"stream_id": 12, "name": "general"}],
        },
        {"result": "success", "msg": "", "subscribers": [1, 2]},
    ]
    sections = [
        "profile",
        "authentication",
        "access",
        "permissions",
        "message_policies",
        "new_user_defaults",
        "default_channels",
        "groups",
        "users",
        "bots",
        "invitations",
    ]

    result = mcp_module.get_organization_configuration(sections=sections)

    assert isinstance(result, ToolResult)
    structured = result.structured_content
    assert structured["zulip_version"] == "12.3"
    assert structured["zulip_feature_level"] == 500
    assert structured["captured_at"].endswith("Z")
    assert structured["principal"]["user_id"] == 1
    assert structured["authority_status"] == {
        "server_settings": "ok",
        "principal": "ok",
    }
    assert structured["section_status"] == {
        name: "empty" if name == "invitations" else "ok" for name in sections
    }
    data = structured["sections"]
    assert data["profile"]["data"]["realm_name"] == "Synthetic realm"
    assert data["authentication"]["data"] == {
        "realm_authentication_methods": {"Email": True},
    }
    assert data["access"]["data"] == {
        "realm_invite_required": True,
        "realm_enable_spectator_access": True,
        "realm_email_changes_disabled": True,
    }
    assert data["message_policies"]["data"] == {
        "realm_message_retention_days": -1,
        "realm_message_content_edit_limit_seconds": 600,
        "realm_message_content_delete_limit_seconds": 300,
        "realm_move_messages_between_streams_limit_seconds": 3600,
        "realm_move_messages_within_stream_limit_seconds": 7200,
        "realm_message_content_allowed_in_email_notifications": False,
        "retention_semantics": "retain_forever",
    }
    permissions = data["permissions"]["data"]
    assert permissions["realm_can_create_public_channel_group"] == 4
    assert permissions["resolved_group_settings"] == {
        "realm_can_create_public_channel_group": {
            "raw": 4,
            "group_name": "administrators",
        },
        "realm_can_delete_own_message_group": {
            "raw": {"direct_members": [], "direct_subgroups": [4]},
            "direct_subgroup_names": ["administrators"],
        },
    }
    assert data["default_channels"]["data"] == {
        "default_streams": [12],
        "default_stream_groups": [],
    }
    assert "delivery_email" not in data["users"]["data"]["members"][0]
    bot = data["bots"]["data"]["bots"][0]
    assert bot["full_name"] == "Synthetic Bot"
    assert bot["subscription_status"] == "ok"
    assert bot["channel_subscriptions"] == [
        {"stream_id": 12, "name": "general"},
    ]
    assert bot["subscription_errors"] == []
    assert "bot-private-key" not in str(result)
    assert client.call_endpoint.call_args_list[2:4] == [
        call(
            url="/register",
            method="POST",
            request={
                "event_types": [],
                "fetch_event_types": [
                    "realm",
                    "realm_user_settings_defaults",
                    "default_streams",
                    "default_stream_groups",
                ],
            },
        ),
        call(
            url="/events",
            method="DELETE",
            request={"queue_id": "queue-1"},
        ),
    ]


def test_queue_failure_does_not_hide_independent_sections(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {
            "result": "error",
            "code": "PERMISSION_DENIED",
            "msg": "not permitted",
        },
        {"result": "success", "msg": "", "user_groups": []},
    ]

    result = mcp_module.get_organization_configuration(
        sections=["profile", "permissions", "groups"],
    ).structured_content

    assert result["section_status"] == {
        "profile": "forbidden",
        "permissions": "forbidden",
        "groups": "empty",
    }
    assert result["sections"]["profile"]["error"]["code"] == "PERMISSION_DENIED"
    assert result["sections"]["groups"]["data"] == {"user_groups": []}


def test_missing_supported_queue_bundles_are_partial(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {
            "result": "success",
            "msg": "",
            "queue_id": "queue-1",
            "zulip_feature_level": 500,
            "default_streams": [],
        },
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_organization_configuration(
        sections=["new_user_defaults", "default_channels"],
    ).structured_content

    assert result["section_status"] == {
        "new_user_defaults": "partial",
        "default_channels": "partial",
    }
    assert result["sections"]["new_user_defaults"]["absent_fields"] == [
        "realm_user_settings_defaults",
    ]
    assert result["sections"]["default_channels"]["absent_fields"] == [
        "default_stream_groups",
    ]


def test_queue_bundles_distinguish_unsupported_null_and_empty(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {
            "result": "success",
            "msg": "",
            "queue_id": "queue-1",
            "zulip_feature_level": 94,
            "default_streams": None,
            "default_stream_groups": [],
        },
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_organization_configuration(
        sections=["new_user_defaults", "default_channels"],
    ).structured_content

    assert result["section_status"] == {
        "new_user_defaults": "unsupported",
        "default_channels": "partial",
    }
    assert result["sections"]["new_user_defaults"]["unsupported_fields"] == [
        "realm_user_settings_defaults",
    ]
    assert result["sections"]["default_channels"]["data"] == {
        "default_streams": None,
        "default_stream_groups": [],
    }


def test_bot_subscription_failures_are_preserved_per_bot(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {
            "result": "success",
            "msg": "",
            "members": [{
                "user_id": 2,
                "full_name": "Synthetic Bot",
                "is_bot": True,
                "is_active": True,
            }],
        },
        {
            "result": "success",
            "msg": "",
            "streams": [
                {"stream_id": 12, "name": "visible"},
                {"stream_id": 13, "name": "restricted"},
            ],
        },
        {"result": "success", "msg": "", "subscribers": [2]},
        {
            "result": "error",
            "code": "PERMISSION_DENIED",
            "msg": "not permitted",
        },
    ]

    result = mcp_module.get_organization_configuration(
        sections=["bots"],
    ).structured_content

    assert result["section_status"] == {"bots": "partial"}
    bot = result["sections"]["bots"]["data"]["bots"][0]
    assert bot["subscription_status"] == "partial"
    assert bot["channel_subscriptions"] == [
        {"stream_id": 12, "name": "visible"},
    ]
    assert bot["subscription_errors"] == [{
        "stream_id": 13,
        "stream_name": "restricted",
        "status": "forbidden",
        "message": "not permitted",
        "code": "PERMISSION_DENIED",
        "http_status": None,
    }]


def test_non_admin_bot_subscription_coverage_is_partial(client: Mock) -> None:
    member_principal = principal_response()
    member_principal.update({"role": 400, "is_owner": False, "is_admin": False})
    client.call_endpoint.side_effect = [
        server_response(),
        member_principal,
        {
            "result": "success",
            "msg": "",
            "members": [{
                "user_id": 2,
                "full_name": "Synthetic Bot",
                "is_bot": True,
                "is_active": True,
            }],
        },
        {
            "result": "success",
            "msg": "",
            "streams": [{"stream_id": 12, "name": "visible"}],
        },
        {"result": "success", "msg": "", "subscribers": [2]},
    ]

    result = mcp_module.get_organization_configuration(
        sections=["bots"],
    ).structured_content

    assert result["section_status"] == {"bots": "partial"}
    bot = result["sections"]["bots"]["data"]["bots"][0]
    assert bot["subscription_status"] == "partial"
    assert bot["subscription_errors"][-1]["code"] == "CHANNEL_VISIBILITY_INCOMPLETE"


def test_inactive_bot_subscriptions_are_explicitly_unavailable(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {
            "result": "success",
            "msg": "",
            "members": [{
                "user_id": 2,
                "full_name": "Inactive Bot",
                "is_bot": True,
                "is_active": False,
            }],
        },
    ]

    result = mcp_module.get_organization_configuration(
        sections=["bots"], include_deactivated=True,
    ).structured_content

    assert result["section_status"] == {"bots": "partial"}
    bot = result["sections"]["bots"]["data"]["bots"][0]
    assert bot["subscription_status"] == "unsupported"
    assert bot["channel_subscriptions"] is None
    assert bot["subscription_errors"][0]["code"] == (
        "INACTIVE_USER_SUBSCRIPTIONS_UNAVAILABLE"
    )
    assert len(client.call_endpoint.call_args_list) == 3


def test_section_selection_avoids_queue_and_unrequested_reads(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server_response(),
        principal_response(),
        {"result": "success", "msg": "", "emoji": {}},
    ]

    result = mcp_module.get_organization_configuration(
        sections=["emoji"],
    ).structured_content

    assert result["section_status"] == {"emoji": "empty"}
    assert [entry.kwargs["url"] for entry in client.call_endpoint.call_args_list] == [
        "/server_settings",
        "/users/me",
        "/realm/emoji",
    ]


def test_unknown_section_is_rejected_before_api_calls(client: Mock) -> None:
    with pytest.raises(ValueError, match="unknown"):
        mcp_module.get_organization_configuration(sections=["unknown"])

    client.call_endpoint.assert_not_called()
