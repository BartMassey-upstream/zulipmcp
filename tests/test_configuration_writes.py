import importlib
import json
from unittest.mock import Mock, call

import pytest
from fastmcp.tools.tool import ToolResult

from zulipmcp import core
from zulipmcp.configuration import REDACTED

mcp_module = importlib.import_module("zulipmcp.mcp")


@pytest.fixture(autouse=True)
def reset_write_authorization() -> None:
    core.set_configuration_writes_enabled(False)
    core.set_user_content_writes_enabled(False)
    yield
    core.set_configuration_writes_enabled(False)
    core.set_user_content_writes_enabled(False)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    core.set_configuration_writes_enabled(True)
    return client


def principal(*, owner: bool = True, admin: bool = True) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "user_id": 1,
        "is_owner": owner,
        "is_admin": admin,
        "is_bot": False,
        "is_active": True,
    }


def realm_snapshot(**values: object) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "queue_id": "queue-1",
        **{f"realm_{key}": value for key, value in values.items()},
    }


def defaults_snapshot(**values: object) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "queue_id": "queue-1",
        "realm_user_settings_defaults": values,
    }


def deleted() -> dict[str, object]:
    return {"result": "success", "msg": ""}


def test_writes_are_disabled_until_confirmed(client: Mock) -> None:
    core.set_configuration_writes_enabled(False)
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(description="old"), deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        {"description": "new"}, expected={"description": "old"},
    )

    assert isinstance(result, ToolResult)
    assert result.structured_content["status"] == "disabled"
    assert result.structured_content["error"]["code"] == (
        "CONFIGURATION_WRITES_DISABLED"
    )
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_enable_configuration_writes() -> None:
    result = mcp_module.enable_configuration_writes()

    assert result.structured_content == {
        "status": "enabled",
        "gate": "configuration",
        "configuration_writes_enabled": True,
        "user_content_writes_enabled": False,
    }


def test_enable_configuration_writes_is_idempotent() -> None:
    core.set_configuration_writes_enabled(True)

    result = mcp_module.enable_configuration_writes()

    assert result.structured_content == {
        "status": "already_enabled",
        "gate": "configuration",
        "configuration_writes_enabled": True,
        "user_content_writes_enabled": False,
    }


def test_disable_configuration_writes() -> None:
    core.set_configuration_writes_enabled(True)
    core.set_user_content_writes_enabled(True)

    result = mcp_module.disable_configuration_writes()

    assert result.structured_content == {
        "status": "disabled",
        "gate": "configuration",
        "configuration_writes_enabled": False,
        "user_content_writes_enabled": True,
    }


def test_user_content_write_actions_are_independent() -> None:
    enabled = mcp_module.enable_user_content_writes()

    assert enabled.structured_content == {
        "status": "enabled",
        "gate": "user_content",
        "configuration_writes_enabled": False,
        "user_content_writes_enabled": True,
    }

    enabled_again = mcp_module.enable_user_content_writes()
    assert enabled_again.structured_content["status"] == "already_enabled"

    disabled = mcp_module.disable_user_content_writes()
    assert disabled.structured_content == {
        "status": "disabled",
        "gate": "user_content",
        "configuration_writes_enabled": False,
        "user_content_writes_enabled": False,
    }


def test_disabled_writes_allow_dry_run(client: Mock) -> None:
    core.set_configuration_writes_enabled(False)
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(description="old"), deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        {"description": "new"},
        expected={"description": "old"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"description": "new"}
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_disabled_writes_allow_no_op(client: Mock) -> None:
    core.set_configuration_writes_enabled(False)
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(description="current"), deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        {"description": "current"}, expected={"description": "current"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"] == {}
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_realm_dry_run_reads_current_and_resolves_optimistic_group_update(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_group = {"direct_members": [], "direct_subgroups": [4]}
    desired_group = {"direct_members": [], "direct_subgroups": ["students"]}
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(description="old", can_create_public_channel_group=current_group),
        deleted(),
        {
            "result": "success",
            "msg": "",
            "user_groups": [{"id": 5, "name": "students"}],
        },
        {"result": "success", "msg": "", "members": []},
    ]

    result = mcp_module.update_organization_configuration(
        changes={
            "description": "new",
            "can_create_public_channel_group": desired_group,
        },
        expected={"description": "old"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["current"] == {
        "description": "old",
        "can_create_public_channel_group": current_group,
    }
    assert result["request"] == {
        "description": "new",
        "can_create_public_channel_group": {
            "new": {"direct_members": [], "direct_subgroups": [5]},
            "old": current_group,
        },
    }
    assert result["changed_fields"] == [
        "can_create_public_channel_group", "description",
    ]
    assert result["resolved_mappings"] == {
        "can_create_public_channel_group": {
            "desired": {
                "semantic": desired_group,
                "resolved": {"direct_members": [], "direct_subgroups": [5]},
            },
        },
    }
    assert [item.kwargs["method"] for item in client.call_endpoint.call_args_list] == [
        "GET", "POST", "DELETE", "GET", "GET",
    ]


def test_expected_value_conflict_makes_no_patch(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(description="changed elsewhere"), deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"description": "desired"},
        expected={"description": "stale"},
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_unknown_and_unsupported_fields_are_rejected_locally(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unknown = mcp_module.update_organization_configuration(
        changes={"not_a_realm_setting": True},
    ).structured_content
    assert unknown["status"] == "error"
    assert unknown["error"]["code"] == "INVALID_FIELD"
    client.call_endpoint.assert_not_called()

    client.call_endpoint.side_effect = [principal(), realm_snapshot(), deleted()]
    unsupported = mcp_module.update_organization_configuration(
        changes={"media_preview_size": 100},
    ).structured_content
    assert unsupported["status"] == "error"
    assert unsupported["error"]["code"] == "UNSUPPORTED_FIELD"
    assert unsupported["unsupported_fields"] == ["media_preview_size"]


def test_owner_only_and_last_authentication_method_checks(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.return_value = principal(owner=False, admin=True)

    owner_only = mcp_module.update_organization_configuration(
        changes={"invite_required": True},
    ).structured_content

    assert owner_only["status"] == "forbidden"
    assert owner_only["error"]["code"] == "OWNER_REQUIRED"

    client.reset_mock()
    client.call_endpoint.return_value = principal()
    lockout = mcp_module.update_organization_configuration(
        changes={"authentication_methods": {"Email": False, "LDAP": False}},
    ).structured_content

    assert lockout["status"] == "error"
    assert lockout["error"]["code"] == "LAST_AUTHENTICATION_METHOD"
    client.call_endpoint.assert_called_once()

    client.reset_mock()
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(authentication_methods={"Email": True, "LDAP": True}),
        deleted(),
    ]
    fake_fallback = mcp_module.update_organization_configuration(
        changes={
            "authentication_methods": {
                "Email": False,
                "LDAP": False,
                "NotARealMethod": True,
            },
        },
        dry_run=True,
    ).structured_content

    assert fake_fallback["status"] == "error"
    assert fake_fallback["error"]["code"] == "LAST_AUTHENTICATION_METHOD"

    for field in ("can_create_groups", "can_manage_all_groups"):
        client.reset_mock()
        client.call_endpoint.side_effect = None
        client.call_endpoint.return_value = principal(owner=False, admin=True)
        denied = mcp_module.update_organization_configuration(
            changes={field: "staff"}, dry_run=True,
        ).structured_content
        assert denied["status"] == "forbidden"
        assert denied["error"]["code"] == "OWNER_REQUIRED"


def test_authentication_change_requires_exact_expected_state(
    client: Mock,
) -> None:
    current = {"Email": True, "LDAP": True}
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(authentication_methods=current),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"authentication_methods": {"Email": True, "LDAP": False}},
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_STATE_REQUIRED"
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_authentication_change_warns_external_health_is_unverified(
    client: Mock,
) -> None:
    current = {"Email": True, "LDAP": True}
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(authentication_methods=current),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"authentication_methods": {"Email": True, "LDAP": False}},
        expected={"authentication_methods": current},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert any("identity provider" in warning for warning in result["warnings"])
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_authentication_change_normalizes_modern_audit_shape(
    client: Mock,
) -> None:
    current = {
        "Email": {"available": True, "enabled": True},
        "LDAP": {"available": True, "enabled": False},
        "GitHub": {"available": False, "enabled": False},
    }
    expected = {"Email": True, "LDAP": False}
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(authentication_methods=current),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"authentication_methods": expected},
        expected={"authentication_methods": current},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {}
    assert any("identity provider" in warning for warning in result["warnings"])


def test_authentication_change_rejects_unavailable_method(
    client: Mock,
) -> None:
    current = {
        "Email": {"available": True, "enabled": True},
        "GitHub": {"available": False, "enabled": False},
    }
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(authentication_methods=current),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={
            "authentication_methods": {"Email": True, "GitHub": True},
        },
        expected={"authentication_methods": current},
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "LAST_AUTHENTICATION_METHOD"


def test_successful_realm_write_has_authoritative_readback(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(description="old"),
        deleted(),
        {"result": "success", "msg": ""},
        realm_snapshot(description="new"),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"description": "new"},
        expected={"description": "old"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"description": "new"}
    assert result["response"] == {}
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm",
        method="PATCH",
        request={"description": "new"},
    )


def test_ignored_parameters_are_partial_not_success(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(description="old"),
        deleted(),
        {
            "result": "success",
            "msg": "",
            "ignored_parameters_unsupported": ["description"],
        },
        realm_snapshot(description="old"),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"description": "new"},
    ).structured_content

    assert result["status"] == "partial"
    assert result["unsupported_fields"] == ["description"]
    assert result["readback"] == {"description": "old"}


@pytest.mark.parametrize("field", sorted(core.UNLIMITED_REALM_FIELDS))
def test_nullable_duration_dry_run_uses_json_encoded_unlimited(
    client: Mock, field: str,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(**{field: 30}),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={field: None},
        expected={field: 30},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {field: '"unlimited"'}


@pytest.mark.parametrize("field", sorted(core.UNLIMITED_REALM_FIELDS))
def test_nullable_duration_changes_from_unlimited_to_finite(
    client: Mock, field: str,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(**{field: None}),
        deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={field: 600},
        expected={field: None},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {field: 600}


def test_nullable_duration_live_request_matches_dry_run_encoding(
    client: Mock,
) -> None:
    fields = sorted(core.UNLIMITED_REALM_FIELDS)
    old = {field: 30 for field in fields}
    new = {field: None for field in fields}
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(**old), deleted(),
    ]
    dry_run = mcp_module.update_organization_configuration(
        changes=new,
        expected=old,
        dry_run=True,
    ).structured_content

    non_mutation_responses = iter([
        principal(), realm_snapshot(**old), deleted(),
        realm_snapshot(**new), deleted(),
    ])
    live_requests: list[dict[str, object]] = []

    def call_endpoint(**kwargs: object) -> dict[str, object]:
        if kwargs["method"] == "PATCH":
            request = kwargs["request"]
            assert isinstance(request, dict)
            live_requests.append(request)
            assert all(json.loads(request[field]) == "unlimited" for field in fields)
            return deleted()
        return next(non_mutation_responses)

    client.call_endpoint.side_effect = call_endpoint
    result = mcp_module.update_organization_configuration(
        changes=new,
        expected=old,
    ).structured_content
    expected_request = {field: '"unlimited"' for field in fields}

    assert result["status"] == "ok"
    assert result["request"] == expected_request
    assert result["request"] == dry_run["request"]
    assert live_requests == [expected_request]
    assert result["readback"] == new


def test_semantic_channel_resolution_rejects_raw_ids(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(signup_announcements_stream_id=12),
        deleted(),
        {
            "result": "success",
            "msg": "",
            "streams": [{"stream_id": 13, "name": "announcements"}],
        },
    ]

    resolved = mcp_module.update_organization_configuration(
        changes={"signup_announcements_stream_id": "announcements"},
        dry_run=True,
    ).structured_content

    assert resolved["status"] == "dry_run"
    assert resolved["request"] == {"signup_announcements_stream_id": 13}
    assert resolved["resolved_mappings"] == {
        "signup_announcements_stream_id": {
            "desired": {"semantic": "announcements", "resolved": 13},
        },
    }

    client.reset_mock()
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(signup_announcements_stream_id=12), deleted(),
        {"result": "success", "msg": "", "streams": []},
    ]
    rejected = mcp_module.update_organization_configuration(
        changes={"signup_announcements_stream_id": 13},
        dry_run=True,
    ).structured_content
    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_channel_reference_clear_uses_minus_one_and_is_idempotent(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(signup_announcements_stream_id=12),
        deleted(),
        {"result": "success", "msg": "", "streams": []},
    ]
    clear = mcp_module.update_organization_configuration(
        changes={"signup_announcements_stream_id": None},
        dry_run=True,
    ).structured_content
    assert clear["status"] == "dry_run"
    assert clear["request"] == {"signup_announcements_stream_id": -1}
    assert clear["resolved_mappings"]["signup_announcements_stream_id"]["desired"] == {
        "semantic": None,
        "resolved": -1,
    }

    client.reset_mock()
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(signup_announcements_stream_id=-1),
        deleted(),
        {"result": "success", "msg": "", "streams": []},
    ]
    no_op = mcp_module.update_organization_configuration(
        changes={"signup_announcements_stream_id": None},
        expected={"signup_announcements_stream_id": None},
        dry_run=True,
    ).structured_content
    assert no_op["status"] == "dry_run"
    assert no_op["request"] == {}
    assert no_op["changed_fields"] == []


def test_anonymous_group_membership_order_is_canonicalized(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        realm_snapshot(can_create_public_channel_group={
            "direct_members": [8, 7],
            "direct_subgroups": [5, 4],
        }),
        deleted(),
        {
            "result": "success",
            "msg": "",
            "user_groups": [
                {"id": 4, "name": "alpha"},
                {"id": 5, "name": "beta"},
            ],
        },
        {
            "result": "success",
            "msg": "",
            "members": [
                {"user_id": 7, "email": "a@example.test"},
                {"user_id": 8, "email": "b@example.test"},
            ],
        },
    ]

    result = mcp_module.update_organization_configuration(
        changes={"can_create_public_channel_group": {
            "direct_members": ["a@example.test", "b@example.test"],
            "direct_subgroups": ["alpha", "beta"],
        }},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {}
    assert result["changed_fields"] == []


def test_invalid_unlimited_value_returns_structured_error(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(), realm_snapshot(message_retention_days=30), deleted(),
    ]

    result = mcp_module.update_organization_configuration(
        changes={"message_retention_days": {"invalid": True}},
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_null_writes_are_rejected_instead_of_silently_dropped(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    realm_result = mcp_module.update_organization_configuration(
        changes={"description": None}, dry_run=True,
    ).structured_content
    assert realm_result["status"] == "error"
    assert realm_result["error"]["code"] == "NULL_WRITE_UNSUPPORTED"

    client.reset_mock()
    default_result = mcp_module.update_default_user_settings(
        changes={"enable_sounds": None}, dry_run=True,
    ).structured_content
    assert default_result["status"] == "error"
    assert default_result["error"]["code"] == "NULL_WRITE_UNSUPPORTED"
    client.call_endpoint.assert_not_called()


def test_default_settings_dry_run_and_successful_readback(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.side_effect = [
        principal(),
        defaults_snapshot(enable_sounds=True),
        deleted(),
    ]
    dry_run = mcp_module.update_default_user_settings(
        changes={"enable_sounds": False},
        expected={"enable_sounds": True},
        dry_run=True,
    ).structured_content
    assert dry_run["status"] == "dry_run"
    assert dry_run["request"] == {"enable_sounds": False}

    client.reset_mock()
    client.call_endpoint.side_effect = [
        principal(),
        defaults_snapshot(enable_sounds=True),
        deleted(),
        {"result": "success", "msg": ""},
        defaults_snapshot(enable_sounds=False),
        deleted(),
    ]
    applied = mcp_module.update_default_user_settings(
        changes={"enable_sounds": False},
        expected={"enable_sounds": True},
    ).structured_content
    assert applied["status"] == "ok"
    assert applied["readback"] == {"enable_sounds": False}
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm/user_settings_defaults",
        method="PATCH",
        request={"enable_sounds": False},
    )


def test_write_results_redact_secret_shaped_values(
    client: Mock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.call_endpoint.return_value = principal()

    result = mcp_module.update_organization_configuration(
        changes={"authentication_methods": {
            "Email": True,
            "api_key": "never-return-this",
        }},
        dry_run=True,
    )

    assert "never-return-this" not in str(result)
    assert result.structured_content["desired"]["authentication_methods"]["api_key"] == (
        REDACTED
    )
