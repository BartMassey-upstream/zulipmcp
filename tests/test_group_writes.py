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
    core.set_configuration_writes_enabled(True)
    request.addfinalizer(lambda: core.set_configuration_writes_enabled(False))
    return client


def server() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "realm_url": REALM_URL,
        "zulip_feature_level": 500,
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


def groups(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "user_groups": items}


def users() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "members": [
            {
                "user_id": 7,
                "full_name": "Synthetic User",
                "email": "user@example.test",
                "is_active": True,
            },
            {
                "user_id": 8,
                "full_name": "Inactive User",
                "email": "inactive@example.test",
                "is_active": False,
            },
        ],
    }


def role_group() -> dict[str, object]:
    return {
        "id": 4,
        "name": "role:members",
        "description": "System role",
        "members": [],
        "direct_subgroup_ids": [],
    }


def course_group(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": 12,
        "name": "course-staff",
        "description": "Course staff",
        "members": [7],
        "direct_subgroup_ids": [4],
        "can_manage_group": 4,
    }
    value.update(changes)
    return value


def semantic_reads(inventory: list[dict[str, object]]) -> list[dict[str, object]]:
    return [groups(inventory), groups(inventory), users()]


def test_create_user_group_dry_run_resolves_all_names(client: Mock) -> None:
    inventory = [role_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
    ]

    result = mcp_module.create_user_group(
        realm_url=REALM_URL,
        name="course-staff",
        description="Course staff",
        members=["user@example.test"],
        subgroups=["role:members"],
        permissions={"can_manage_group": "role:members"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "name": "course-staff",
        "description": "Course staff",
        "members": [7],
        "subgroups": [4],
        "can_manage_group": 4,
    }
    assert result["resolved_mappings"]["members"] == [
        {"semantic": "user@example.test", "resolved": 7},
    ]


def test_create_user_group_is_idempotent_by_name(client: Mock) -> None:
    inventory = [role_group(), course_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
    ]

    result = mcp_module.create_user_group(
        realm_url=REALM_URL,
        name="course-staff",
        description="Course staff",
        members=["Synthetic User"],
        subgroups=["role:members"],
        permissions={"can_manage_group": "role:members"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"] == {}
    assert "already exists" in result["warnings"][0]


def test_create_user_group_applies_and_reads_back(client: Mock) -> None:
    before = [role_group()]
    after = [role_group(), course_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(before),
        {"result": "success", "msg": "", "group_id": 12},
        groups(after),
    ]

    result = mcp_module.create_user_group(
        realm_url=REALM_URL,
        name="course-staff",
        description="Course staff",
        members=["user@example.test"],
        subgroups=["role:members"],
        permissions={"can_manage_group": "role:members"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == course_group()
    assert client.call_endpoint.call_args_list[5] == call(
        url="/user_groups/create",
        method="POST",
        request={
            "name": "course-staff",
            "description": "Course staff",
            "members": [7],
            "subgroups": [4],
            "can_manage_group": 4,
        },
    )


def test_create_user_group_rejects_inactive_member(client: Mock) -> None:
    inventory = [role_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
    ]

    result = mcp_module.create_user_group(
        realm_url=REALM_URL,
        name="course-staff",
        description="Course staff",
        members=["inactive@example.test"],
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_update_user_group_uses_optimistic_permission_value(client: Mock) -> None:
    inventory = [role_group(), course_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
    ]

    result = mcp_module.update_user_group(
        realm_url=REALM_URL,
        group="course-staff",
        changes={
            "description": "Teaching team",
            "can_manage_group": {
                "direct_members": ["user@example.test"],
                "direct_subgroups": [],
            },
        },
        expected={"description": "Course staff"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/user_groups/12"
    assert result["request"] == {
        "description": "Teaching team",
        "can_manage_group": {
            "old": 4,
            "new": {"direct_members": [7], "direct_subgroups": []},
        },
    }


def test_update_user_group_expected_conflict(client: Mock) -> None:
    inventory = [role_group(), course_group(description="Changed")]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
    ]

    result = mcp_module.update_user_group(
        realm_url=REALM_URL,
        group="course-staff",
        changes={"description": "Teaching team"},
        expected={"description": "Course staff"},
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    assert all(
        item.kwargs["method"] != "PATCH"
        for item in client.call_endpoint.call_args_list
    )


def test_set_user_group_members_dry_run_calculates_delta(client: Mock) -> None:
    inventory = [role_group(), course_group(members=[])]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
        {"result": "success", "msg": "", "members": []},
    ]

    result = mcp_module.set_user_group_members(
        realm_url=REALM_URL,
        group="course-staff",
        members=["Synthetic User"],
        subgroups=[],
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "add": [7],
        "delete_subgroups": [4],
    }


def test_set_user_group_members_omitted_subgroups_preserves_them(client: Mock) -> None:
    inventory = [role_group(), course_group(members=[])]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(inventory),
        {"result": "success", "msg": "", "members": []},
    ]

    result = mcp_module.set_user_group_members(
        realm_url=REALM_URL,
        group="course-staff",
        members=["Synthetic User"],
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"add": [7]}
    assert "subgroups" not in result["desired"]


def test_set_user_group_members_applies_and_reads_back(client: Mock) -> None:
    before = [role_group(), course_group(members=[], direct_subgroup_ids=[])]
    after = [role_group(), course_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(before),
        {"result": "success", "msg": "", "members": []},
        {"result": "success", "msg": ""},
        {"result": "success", "msg": "", "members": [7]},
        groups(after),
    ]

    result = mcp_module.set_user_group_members(
        realm_url=REALM_URL,
        group="course-staff",
        members=["user@example.test"],
        subgroups=["role:members"],
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"member_ids": [7], "subgroup_ids": [4]}
    assert client.call_endpoint.call_args_list[6] == call(
        url="/user_groups/12/members",
        method="POST",
        request={"add": [7], "add_subgroups": [4]},
    )


def test_set_user_group_members_reports_ignored_parameters(client: Mock) -> None:
    before = [role_group(), course_group(members=[], direct_subgroup_ids=[])]
    after = [role_group(), course_group()]
    client.call_endpoint.side_effect = [
        server(), principal(), *semantic_reads(before),
        {"result": "success", "msg": "", "members": []},
        {
            "result": "success",
            "msg": "",
            "ignored_parameters_unsupported": ["add_subgroups"],
        },
        {"result": "success", "msg": "", "members": [7]},
        groups(after),
    ]

    result = mcp_module.set_user_group_members(
        realm_url=REALM_URL,
        group="course-staff",
        members=["user@example.test"],
        subgroups=["role:members"],
    ).structured_content

    assert result["status"] == "partial"
    assert result["unsupported_fields"] == ["add_subgroups"]
