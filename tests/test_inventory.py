import importlib
from collections.abc import Callable
from unittest.mock import Mock, call

import pytest
from fastmcp.tools.tool import ToolResult

from zulipmcp import core
from zulipmcp.configuration import REDACTED

mcp_module = importlib.import_module("zulipmcp.mcp")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


def test_list_streams_returns_full_typed_channels(client: Mock) -> None:
    client.call_endpoint.return_value = {
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
    }

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
    }
    assert "is_default" not in result.structured_content["data"]["streams"][1]
    client.call_endpoint.assert_called_once_with(
        url="/streams",
        method="GET",
        request={
            "include_all": True,
            "include_default": True,
            "exclude_archived": False,
        },
    )


def test_list_streams_passes_options_and_omits_missing_selected_fields(
    client: Mock,
) -> None:
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "streams": [{"name": "public", "is_web_public": True}],
    }

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
    client.call_endpoint.assert_called_once_with(
        url="/streams",
        method="GET",
        request={
            "include_all": False,
            "include_default": False,
            "include_web_public": True,
            "exclude_archived": True,
        },
    )


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
    client.call_endpoint.return_value = {
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
    }

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
            "default_sending_stream": 12,
        }],
    }
    assert "bot-private-key" not in str(result)
