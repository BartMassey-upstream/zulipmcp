import importlib
from unittest.mock import Mock

import pytest

from zulipmcp import core
from zulipmcp.capabilities import evaluate_administration_capabilities
from zulipmcp.configuration import APIError, ZulipAPIError

mcp_module = importlib.import_module("zulipmcp.mcp")


def _capability(result: dict[str, object], capability_id: str) -> dict[str, object]:
    data = result["data"]
    assert isinstance(data, dict)
    capabilities = data["capabilities"]
    assert isinstance(capabilities, list)
    matches = [
        item for item in capabilities
        if isinstance(item, dict) and item.get("id") == capability_id
    ]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


def test_capabilities_evaluate_feature_authority_and_policy(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "zulip_version": "12.3",
            "zulip_feature_level": 500,
        },
        {
            "result": "success",
            "msg": "",
            "user_id": 1,
            "is_owner": True,
            "is_admin": True,
            "is_active": True,
        },
        {
            "result": "success",
            "msg": "",
            "queue_id": "queue-1",
            "realm_owner_full_content_access": True,
            "realm_moderation_request_channel_id": 12,
        },
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_administration_capabilities().structured_content

    assert result["status"] == "ok"
    assert result["data"]["zulip_version"] == "12.3"
    full = _capability(result, "exports.full_without_consent")
    assert full["support"] == "supported"
    assert full["authority_status"] == "available"
    assert full["required_server_field_status"] is True
    folders = _capability(result, "channels.folder_order")
    assert folders["support"] == "supported"
    realm = _capability(result, "organization.deactivate")
    assert realm["support"] == "not_implemented"
    assert realm["server_support"] == "supported"
    assert "data_exports" in result["data"]["supported_audit_sections"]
    assert "authentication" in result["data"]["organization_snapshot_sections"]


def test_capabilities_distinguish_server_support_and_principal_authority(
    client: Mock,
) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success",
            "msg": "",
            "zulip_version": "10.0",
            "zulip_feature_level": 400,
        },
        {
            "result": "success",
            "msg": "",
            "user_id": 2,
            "is_owner": False,
            "is_admin": False,
            "is_active": True,
        },
        {
            "result": "success",
            "msg": "",
            "queue_id": "queue-2",
            "realm_moderation_request_channel_id": -1,
        },
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_administration_capabilities().structured_content

    folder_order = _capability(result, "channels.folder_order")
    assert folder_order["support"] == "unsupported"
    assert folder_order["server_support"] == "unsupported"
    authentication = _capability(result, "organization.authentication")
    assert authentication["support"] == "conditional"
    assert authentication["server_support"] == "supported"
    assert authentication["authority_status"] == "insufficient"
    assert result["data"]["authority_status"]["realm_fields"] == "ok"
    assert _capability(result, "channels.create")["support"] == "unsupported"
    assert _capability(result, "channels.archive")["support"] == "conditional"
    assert _capability(result, "channels.unarchive")["support"] == "conditional"
    assert _capability(result, "channels.defaults")["support"] == "conditional"
    assert _capability(result, "emoji.lifecycle")["support"] == "conditional"
    assert _capability(result, "moderation.delete_message")["support"] == (
        "conditional"
    )
    report = _capability(result, "moderation.report_message")
    assert report["support"] == "conditional"
    assert report["required_server_field_status"] == -1
    full = _capability(result, "exports.full_without_consent")
    assert full["support"] == "unsupported"
    assert full["server_support"] == "unsupported"


def test_capabilities_preserve_catalog_when_one_audit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_server_settings",
        lambda: (_ for _ in ()).throw(ZulipAPIError(APIError(
            message="server unavailable", code="TRANSPORT_ERROR",
        ))),
    )
    monkeypatch.setattr(
        core,
        "get_current_user",
        lambda: {"user_id": 1, "is_owner": True, "is_active": True},
    )

    result = mcp_module.get_administration_capabilities().structured_content

    assert result["status"] == "partial"
    assert result["data"]["authority_status"] == {
        "server_settings": "error",
        "principal": "ok",
        "realm_fields": "unknown",
    }
    assert _capability(result, "channels.folder_order")["support"] == "unknown"
    assert any("server unavailable" in warning for warning in result["warnings"])


def test_capability_catalog_reports_intentional_gaps(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        {
            "result": "success", "msg": "", "zulip_feature_level": 500,
        },
        {
            "result": "success", "msg": "", "user_id": 1,
            "is_owner": True, "is_admin": True, "is_active": True,
        },
        {
            "result": "success", "msg": "", "queue_id": "queue-3",
            "realm_owner_full_content_access": True,
            "realm_moderation_request_channel_id": 12,
        },
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_administration_capabilities().structured_content

    reusable = _capability(result, "invitations.reusable_create")
    assert reusable["support"] == "not_implemented"
    assert "bearer-link" in reusable["known_gaps"][0]
    moderation = _capability(result, "moderation.report_workflow")
    assert moderation["support"] == "unsupported"
    assert "resolution API" in moderation["known_gaps"][0]
    reactivation = _capability(result, "organization.reactivate")
    assert reactivation["support"] == "unsupported"
    assert "management command" in reactivation["known_gaps"][0]


def test_action_level_feature_floors_are_evaluated_independently() -> None:
    result = {
        "data": evaluate_administration_capabilities(
            {"zulip_feature_level": 300},
            {"is_owner": True, "is_admin": True, "is_active": True},
            {"realm_moderation_request_channel_id": -1},
        ),
    }

    assert _capability(result, "groups.deactivate")["support"] == "supported"
    assert _capability(result, "groups.reactivate")["support"] == "unsupported"
    assert _capability(result, "channels.archive")["support"] == "unsupported"
    assert _capability(result, "channels.defaults")["support"] == "supported"
