import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import SectionResult, SectionStatus

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"


def group(*, deactivated: bool = False) -> dict[str, object]:
    return {
        "id": 12,
        "name": "course-staff",
        "description": "Course staff",
        "members": [7],
        "direct_subgroup_ids": [],
        "deactivated": deactivated,
    }


def bot_item(*, active: bool = True) -> dict[str, object]:
    return {
        "user_id": 22,
        "email": "helper-bot@example.test",
        "full_name": "Helper",
        "is_bot": True,
        "is_active": active,
        "owner": {"email": "owner@example.test"},
        "channel_subscriptions": [{"stream_id": 10, "name": "general"}],
        "subscription_status": "ok",
        "default_sending_channel": "general",
        "default_events_register_channel": None,
    }


def bot_audit(item: dict[str, object]) -> SectionResult:
    return SectionResult(SectionStatus.OK, data={"bots": [item]})


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(True)
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 500},
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    yield
    core.set_configuration_writes_enabled(False)


def test_group_deactivation_dry_run_includes_impact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_group_inventory", lambda: [group()])
    monkeypatch.setattr(
        core, "_group_lifecycle_dependencies", lambda group_id, groups: ([], []),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_user_group_active(
        REALM_URL, "course-staff", False, expected_active=True, dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/user_groups/12/deactivate"
    assert result["request"] == {}
    assert result["resolved_mappings"]["impact"]["members"] == [7]
    mutation.assert_not_called()


def test_group_deactivation_rejects_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_group_inventory", lambda: [group()])
    monkeypatch.setattr(
        core,
        "_group_lifecycle_dependencies",
        lambda group_id, groups: ([{
            "kind": "organization_permission", "field": "can_create_groups",
        }], []),
    )

    result = mcp_module.set_user_group_active(
        REALM_URL, "course-staff", False, dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "USER_GROUP_IN_USE"


def test_group_dependency_audit_includes_channel_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_read_write_state", lambda fields, defaults: ({}, []))
    monkeypatch.setattr(
        core,
        "get_streams_configuration",
        lambda **kwargs: {"streams": [{
            "stream_id": 44,
            "name": "staff",
            "can_send_message_group": {
                "direct_members": [],
                "direct_subgroups": [12],
            },
        }]},
    )

    dependencies, warnings = core._group_lifecycle_dependencies(12, [group()])

    assert warnings == []
    assert dependencies == [{
        "kind": "channel_permission",
        "channel": "staff",
        "channel_id": 44,
        "field": "can_send_message_group",
    }]


def test_group_reactivation_applies_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_group_inventory",
        Mock(side_effect=[[group(deactivated=True)], [group(deactivated=False)]]),
    )
    monkeypatch.setattr(
        core, "_group_lifecycle_dependencies", lambda group_id, groups: ([], []),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_user_group_active(
        REALM_URL, "course-staff", True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"is_active": True}
    mutation.assert_called_once_with(
        "/user_groups/12", "PATCH", {"deactivated": False},
    )


def test_group_reactivation_checks_feature_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 385},
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )

    result = mcp_module.set_user_group_active(
        REALM_URL, "course-staff", True, dry_run=True,
    ).structured_content

    assert result["status"] == "unsupported"
    assert result["error"]["code"] == "UNSUPPORTED_FEATURE"


def test_bot_deactivation_dry_run_preserves_configuration_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = bot_item()
    monkeypatch.setattr(core, "_bot_audit_item", lambda reference: (bot_audit(item), item))
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_bot_active(
        REALM_URL,
        "helper-bot@example.test",
        False,
        expected_active=True,
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/bots/22"
    impact = result["resolved_mappings"]["impact"]
    assert impact["configuration_preserved"] is True
    assert impact["channel_subscriptions"] == [
        {"stream_id": 10, "name": "general"},
    ]
    mutation.assert_not_called()


def test_bot_reactivation_applies_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = bot_item(active=False)
    after = bot_item(active=True)
    monkeypatch.setattr(
        core,
        "_bot_audit_item",
        Mock(side_effect=[(bot_audit(before), before), (bot_audit(after), after)]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_bot_active(
        REALM_URL, "helper-bot@example.test", True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"is_active": True}
    assert mutation.call_args_list == [call("/users/22/reactivate", "POST", {})]


def test_bot_lifecycle_gate_rejection_is_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = bot_item()
    monkeypatch.setattr(core, "_bot_audit_item", lambda reference: (bot_audit(item), item))
    core.set_configuration_writes_enabled(False)

    result = mcp_module.set_bot_active(
        REALM_URL, "helper-bot@example.test", False,
    ).structured_content

    assert result["status"] == "disabled"
    assert result["error"]["code"] == "CONFIGURATION_WRITES_DISABLED"
