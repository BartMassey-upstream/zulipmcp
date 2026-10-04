import asyncio
import importlib
from collections.abc import Callable
from unittest.mock import Mock, call

import pytest
from fastmcp.tools.tool import ToolResult

from zulipmcp import core
from zulipmcp.capabilities import CAPABILITY_DEFINITIONS
from zulipmcp.configuration import REDACTED

mcp_module = importlib.import_module("zulipmcp.mcp")


READ_ONLY_TOOLS = {
    "get_administration_capabilities",
    "get_allowed_domains",
    "get_bots",
    "get_channel_folders",
    "get_current_user",
    "get_custom_emoji",
    "get_custom_profile_fields",
    "get_data_exports",
    "get_invitations",
    "get_linkifiers",
    "get_message_by_id",
    "get_message_link",
    "get_moderation_configuration",
    "get_messages",
    "get_organization_branding",
    "get_organization_configuration",
    "get_server_settings",
    "get_stream_members",
    "get_stream_topics",
    "get_subscribed_streams",
    "get_user_groups",
    "get_user_info",
    "get_users",
    "list_emoji",
    "list_streams",
    "resolve_name",
    "verify_message",
}

DESTRUCTIVE_TOOLS = {
    "archive_channel",
    "create_bot",
    "create_channel",
    "deactivate_custom_emoji",
    "delete_custom_profile_field",
    "delete_data_export",
    "delete_message_for_moderation",
    "edit_message",
    "end_session",
    "move_messages",
    "remove_reaction",
    "remove_allowed_domain",
    "remove_linkifier",
    "revoke_email_invitation",
    "revoke_reusable_invitation",
    "reply",
    "resolve_topic",
    "send_direct_message",
    "send_message",
    "set_bot_channel_subscriptions",
    "set_bot_active",
    "set_channel_archived",
    "set_channel_folder",
    "set_channel_folder_order",
    "set_channel_members",
    "set_default_channel",
    "set_default_channels",
    "set_user_group_members",
    "set_user_active",
    "set_user_group_active",
    "unsubscribe_users_from_channel",
    "update_allowed_domain",
    "update_bot_configuration",
    "update_channel_configuration",
    "update_channel_folder",
    "update_custom_profile_field",
    "update_default_user_settings",
    "update_linkifier",
    "update_organization_configuration",
    "update_user_group",
    "update_user_configuration",
    "upload_file",
    "upload_organization_branding",
}

CONFIGURATION_WRITE_TOOLS = {
    "add_allowed_domain",
    "archive_channel",
    "create_bot",
    "create_channel",
    "create_channel_folder",
    "create_custom_profile_field",
    "create_data_export",
    "create_linkifier",
    "delete_custom_profile_field",
    "delete_data_export",
    "create_user_group",
    "deactivate_custom_emoji",
    "invite_users",
    "resend_email_invitation",
    "remove_allowed_domain",
    "remove_linkifier",
    "revoke_email_invitation",
    "revoke_reusable_invitation",
    "set_bot_channel_subscriptions",
    "set_bot_active",
    "set_channel_archived",
    "set_channel_folder",
    "set_channel_folder_order",
    "set_channel_members",
    "set_default_channel",
    "set_default_channels",
    "set_user_group_members",
    "set_user_active",
    "set_user_group_active",
    "subscribe_users_to_channel",
    "unsubscribe_users_from_channel",
    "update_allowed_domain",
    "update_bot_configuration",
    "update_channel_configuration",
    "update_channel_folder",
    "update_custom_profile_field",
    "update_default_user_settings",
    "update_linkifier",
    "update_organization_configuration",
    "update_user_group",
    "update_user_configuration",
    "upload_custom_emoji",
    "upload_organization_branding",
}

USER_CONTENT_WRITE_TOOLS = {
    "add_reaction",
    "delete_message_for_moderation",
    "edit_message",
    "move_messages",
    "remove_reaction",
    "report_message",
    "reply",
    "resolve_topic",
    "send_direct_message",
    "send_message",
    "upload_file",
}

CONDITIONAL_USER_CONTENT_WRITE_TOOLS = {"end_session"}

UNGATED_LOCAL_OR_TRANSIENT_TOOLS = {
    "disable_configuration_writes",
    "disable_user_content_writes",
    "download_organization_branding",
    "enable_configuration_writes",
    "enable_user_content_writes",
    "fetch_file",
    "fetch_image",
    "listen",
    "set_context",
    "stop_typing",
    "typing",
}

OPEN_WORLD_TOOLS = {
    "create_data_export",
    "invite_users",
    "report_message",
    "resend_email_invitation",
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


def test_all_tools_have_audited_safety_annotations() -> None:
    tools = asyncio.run(mcp_module.mcp.list_tools())
    annotations = {tool.name: tool.annotations for tool in tools}

    assert all(annotation is not None for annotation in annotations.values())
    assert {
        name for name, annotation in annotations.items()
        if annotation.readOnlyHint
    } == READ_ONLY_TOOLS
    assert {
        name for name, annotation in annotations.items()
        if annotation.destructiveHint
    } == DESTRUCTIVE_TOOLS
    assert {
        name for name, annotation in annotations.items()
        if annotation.openWorldHint
    } == OPEN_WORLD_TOOLS


def test_all_non_read_tools_have_an_audited_write_gate_classification() -> None:
    tools = asyncio.run(mcp_module.mcp.list_tools())
    non_read_tools = {
        tool.name for tool in tools if not tool.annotations.readOnlyHint
    }

    classifications = [
        CONFIGURATION_WRITE_TOOLS,
        USER_CONTENT_WRITE_TOOLS,
        CONDITIONAL_USER_CONTENT_WRITE_TOOLS,
        UNGATED_LOCAL_OR_TRANSIENT_TOOLS,
    ]
    assert set().union(*classifications) == non_read_tools
    assert sum(len(group) for group in classifications) == len(non_read_tools)


def test_capability_catalog_references_registered_tools() -> None:
    registered = {
        tool.name for tool in asyncio.run(mcp_module.mcp.list_tools())
    }
    referenced_audits = {
        name
        for item in CAPABILITY_DEFINITIONS
        for name in item.get("audit_tools", ())
    }
    referenced_mutations = {
        name
        for item in CAPABILITY_DEFINITIONS
        for name in item.get("mutation_tools", ())
    }

    assert referenced_audits <= registered
    assert referenced_mutations <= registered
    assert CONFIGURATION_WRITE_TOOLS <= referenced_mutations
    assert {
        "delete_message_for_moderation", "report_message",
    } <= referenced_mutations


def test_write_gate_actions_have_no_parameters() -> None:
    tools = {
        tool.name: tool for tool in asyncio.run(mcp_module.mcp.list_tools())
    }

    for name in {
        "enable_configuration_writes",
        "disable_configuration_writes",
        "enable_user_content_writes",
        "disable_user_content_writes",
    }:
        assert tools[name].parameters == {
            "additionalProperties": False,
            "properties": {},
            "type": "object",
        }


def test_setting_write_tools_require_explicit_realm_url() -> None:
    tools = {
        tool.name: tool for tool in asyncio.run(mcp_module.mcp.list_tools())
    }

    for name in {
        "update_organization_configuration",
        "update_default_user_settings",
    }:
        assert "realm_url" in tools[name].parameters["required"]


def test_list_streams_returns_full_typed_channels(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "streams": [
                {
                    "stream_id": 12,
                    "name": "general",
                    "is_default": True,
                    "is_archived": False,
                    "message_retention_days": None,
                    "can_send_message_group": {
                        "direct_members": [7],
                        "direct_subgroups": [4],
                    },
                },
                {
                    "stream_id": 13,
                    "name": "archive",
                    "is_archived": True,
                    "message_retention_days": -1,
                },
            ],
        },
        {
            "result": "success",
            "msg": "",
            "user_groups": [{"id": 4, "name": "staff"}],
        },
        {
            "result": "success",
            "msg": "",
            "zulip_feature_level": 500,
        },
    ]

    result = mcp_module.list_streams()

    assert isinstance(result, ToolResult)
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["data"]["streams"][0] == {
        "stream_id": 12,
        "name": "general",
        "is_default": True,
        "is_archived": False,
        "message_retention_days": None,
        "can_send_message_group": {
            "direct_members": [7],
            "direct_subgroups": [4],
        },
        "retention_semantics": "inherit_realm_policy",
        "resolved_group_settings": {
            "can_send_message_group": {
                "raw": {"direct_members": [7], "direct_subgroups": [4]},
                "direct_subgroup_names": ["staff"],
            },
        },
    }
    assert "is_default" not in result.structured_content["data"]["streams"][1]
    assert result.structured_content["data"]["streams"][1]["retention_semantics"] == (
        "retain_forever"
    )
    assert result.structured_content["unsupported_fields"] == [
        "streams[].default_push_notifications",
    ]
    assert client.call_endpoint.call_args_list == [
        call(
            url="/streams",
            method="GET",
            request={
                "include_all": True,
                "include_default": True,
                "exclude_archived": False,
            },
        ),
        call(
            url="/user_groups",
            method="GET",
            request={"include_deactivated_groups": True},
        ),
        call(url="/server_settings", method="GET", request=None),
    ]


def test_list_streams_passes_options_and_omits_missing_selected_fields(
    client: Mock,
) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "streams": [{"name": "public", "is_web_public": True}],
        },
        {"result": "success", "msg": "", "user_groups": []},
        {
            "result": "success",
            "msg": "",
            "zulip_feature_level": 500,
        },
    ]

    result = mcp_module.list_streams(
        include_all=False,
        include_default=False,
        include_web_public=True,
        exclude_archived=True,
        fields=["name", "is_default", "is_web_public"],
    )

    assert result.structured_content["data"]["streams"] == [
        {"name": "public", "is_web_public": True},
    ]
    assert client.call_endpoint.call_args_list == [
        call(
            url="/streams",
            method="GET",
            request={
                "include_all": False,
                "include_default": False,
                "include_web_public": True,
                "exclude_archived": True,
            },
        ),
        call(
            url="/user_groups",
            method="GET",
            request={"include_deactivated_groups": True},
        ),
        call(url="/server_settings", method="GET", request=None),
    ]


def test_list_streams_distinguishes_empty_from_forbidden(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success", "msg": "", "streams": [],
    }
    assert mcp_module.list_streams().structured_content["status"] == "empty"

    client.call_endpoint.return_value = {
        "result": "error", "code": "PERMISSION_DENIED", "msg": "denied",
    }
    forbidden = mcp_module.list_streams().structured_content
    assert forbidden["status"] == "forbidden"
    assert forbidden["data"] is None


def test_supported_channel_field_omission_is_partial(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "streams": [{"stream_id": 12, "name": "general"}],
        },
        {"result": "success", "msg": "", "user_groups": []},
        {
            "result": "success",
            "msg": "",
            "zulip_feature_level": 507,
        },
    ]

    result = mcp_module.list_streams().structured_content

    assert result["status"] == "partial"
    assert result["absent_fields"] == [
        "streams[].default_push_notifications",
    ]
    assert result["unsupported_fields"] == []


@pytest.mark.parametrize(
    "response,status,absent_fields",
    [
        ({}, "partial", ["user_groups"]),
        ({"user_groups": None}, "partial", []),
        ({"user_groups": []}, "empty", []),
    ],
)
def test_collection_missing_null_and_empty_are_distinct(
    client: Mock,
    response: dict[str, object],
    status: str,
    absent_fields: list[str],
) -> None:
    client.call_endpoint.return_value = {"result": "success", "msg": "", **response}

    result = mcp_module.get_user_groups().structured_content

    assert result["status"] == status
    assert result["data"] == response
    assert result["absent_fields"] == absent_fields


@pytest.mark.parametrize(
    "members,expected_data,expected_status,expected_absent",
    [
        pytest.param("missing", {}, "partial", ["bots"], id="missing"),
        pytest.param(None, {"bots": None}, "partial", [], id="null"),
        pytest.param([], {"bots": []}, "empty", [], id="empty"),
    ],
)
def test_bot_inventory_preserves_missing_null_and_empty(
    client: Mock,
    members: object,
    expected_data: dict[str, object],
    expected_status: str,
    expected_absent: list[str],
) -> None:
    response: dict[str, object] = {"result": "success", "msg": ""}
    if members != "missing":
        response["members"] = members
    client.call_endpoint.return_value = response

    result = mcp_module.get_bots().structured_content

    assert result["status"] == expected_status
    assert result["data"] == expected_data
    assert result["absent_fields"] == expected_absent


@pytest.mark.parametrize(
    "tool,endpoint,expected_request,field,response",
    [
        (
            mcp_module.get_user_groups,
            "/user_groups",
            {"include_deactivated_groups": True},
            "user_groups",
            [{"id": 4, "name": "staff", "members": [7]}],
        ),
        (
            mcp_module.get_custom_profile_fields,
            "/realm/profile_fields",
            None,
            "custom_fields",
            [{"id": 1, "name": "Pronouns", "field_type": 1}],
        ),
        (
            mcp_module.get_allowed_domains,
            "/realm/domains",
            None,
            "domains",
            [{"domain": "example.test", "allow_subdomains": False}],
        ),
        (
            mcp_module.get_linkifiers,
            "/realm/linkifiers",
            None,
            "linkifiers",
            [{"id": 3, "pattern": "T-(?P<id>[0-9]+)"}],
        ),
        (
            mcp_module.get_custom_emoji,
            "/realm/emoji",
            None,
            "emoji",
            {"party": {"id": "1", "deactivated": False}},
        ),
        (
            mcp_module.get_invitations,
            "/invites",
            None,
            "invites",
            [{"id": 8, "email": "invitee@example.test"}],
        ),
    ],
)
def test_direct_inventory_tools_return_typed_collections(
    client: Mock,
    tool: Callable[[], ToolResult],
    endpoint: str,
    expected_request: dict[str, object] | None,
    field: str,
    response: object,
) -> None:
    client.call_endpoint.return_value = {
        "result": "success", "msg": "", field: response,
    }

    result = tool()

    assert result.structured_content["status"] == "ok"
    assert result.structured_content["data"][field] == response
    client.call_endpoint.assert_called_once_with(
        url=endpoint, method="GET", request=expected_request,
    )


def test_users_hide_sensitive_fields_by_default(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "members": [{
            "user_id": 7,
            "full_name": "Synthetic User",
            "email": "display@example.test",
            "is_bot": False,
            "profile_data": {"1": {"value": "private value"}},
            "delivery_email": "private@example.test",
            "timezone": "UTC",
        }],
    }

    result = mcp_module.get_users()

    assert result.structured_content["data"]["members"] == [{
        "user_id": 7,
        "full_name": "Synthetic User",
        "email": "display@example.test",
        "is_bot": False,
    }]
    client.call_endpoint.assert_called_once_with(
        url="/users",
        method="GET",
        request={"include_custom_profile_fields": True},
    )


def test_users_filter_deactivated_records_locally(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "members": [
            {"user_id": 1, "is_active": True},
            {"user_id": 2, "is_active": False},
        ],
    }

    active = mcp_module.get_users()
    all_users = mcp_module.get_users(include_deactivated=True)

    assert active.structured_content["data"]["members"] == [
        {"user_id": 1, "is_active": True},
    ]
    assert all_users.structured_content["data"]["members"] == [
        {"user_id": 1, "is_active": True},
        {"user_id": 2, "is_active": False},
    ]
    assert client.call_endpoint.call_args_list == [
        call(
            url="/users",
            method="GET",
            request={"include_custom_profile_fields": True},
        ),
        call(
            url="/users",
            method="GET",
            request={"include_custom_profile_fields": True},
        ),
    ]


def test_users_can_include_sensitive_fields_but_still_redact_secrets(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "members": [{
            "user_id": 7,
            "delivery_email": "private@example.test",
            "profile_data": {"1": {"value": "private value"}},
            "api_key": "never-return-this",
        }],
    }

    result = mcp_module.get_users(include_sensitive_user_fields=True)
    member = result.structured_content["data"]["members"][0]

    assert member["delivery_email"] == "private@example.test"
    assert member["profile_data"] == {"1": {"value": "private value"}}
    assert member["api_key"] == REDACTED
    assert "never-return-this" not in str(result)


def test_bots_return_only_safe_audit_metadata(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "user_id": 1,
            "is_owner": True,
            "is_admin": True,
            "is_bot": False,
            "is_active": True,
        },
        {
            "result": "success",
            "msg": "",
            "members": [
                {"user_id": 1, "full_name": "Human", "is_bot": False},
                {
                    "user_id": 2,
                    "full_name": "Audit Bot",
                    "email": "bot@example.test",
                    "is_bot": True,
                    "is_active": True,
                    "bot_type": 1,
                    "bot_owner_id": 1,
                    "default_sending_stream": 12,
                    "api_key": "bot-private-key",
                    "services": [{"config_data": {"token": "private"}}],
                },
            ],
        },
        {
            "result": "success",
            "msg": "",
            "streams": [{"stream_id": 12, "name": "general"}],
        },
        {"result": "success", "msg": "", "subscribers": [2]},
    ]

    result = mcp_module.get_bots()

    assert result.structured_content["data"] == {
        "bots": [{
            "user_id": 2,
            "full_name": "Audit Bot",
            "email": "bot@example.test",
            "is_bot": True,
            "is_active": True,
            "bot_type": 1,
            "bot_owner_id": 1,
            "owner": {"user_id": 1, "full_name": "Human"},
            "default_sending_stream": 12,
            "default_sending_channel": "general",
            "subscription_status": "ok",
            "channel_subscriptions": [{"stream_id": 12, "name": "general"}],
            "subscription_errors": [],
        }],
    }
    assert "bot-private-key" not in str(result)
