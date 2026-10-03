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


def server(feature_level: int = 500) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "realm_url": REALM_URL,
        "zulip_feature_level": feature_level,
    }


def principal(owner: bool = True) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "user_id": 1,
        "is_owner": owner,
        "is_admin": True,
        "is_bot": False,
        "is_active": True,
    }


def profile_fields(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "custom_fields": items}


def domains(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "domains": items}


def linkifiers(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "linkifiers": items}


def test_create_profile_field_dry_run_and_feature_gate(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal(), profile_fields([])]

    result = mcp_module.create_custom_profile_field(
        realm_url=REALM_URL,
        name="Graduation year",
        field_type=1,
        settings={"hint": "YYYY", "use_for_user_matching": True},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "name": "Graduation year",
        "field_type": 1,
        "hint": "YYYY",
        "use_for_user_matching": True,
    }

    client.reset_mock()
    client.call_endpoint.side_effect = [server(454), principal()]
    unsupported = mcp_module.create_custom_profile_field(
        realm_url=REALM_URL,
        name="Graduation year",
        field_type=1,
        settings={"use_for_user_matching": True},
        dry_run=True,
    ).structured_content
    assert unsupported["status"] == "unsupported"
    assert unsupported["unsupported_fields"] == ["use_for_user_matching"]


def test_create_profile_field_is_idempotent_by_name(client: Mock) -> None:
    existing = {
        "id": 9,
        "name": "Graduation year",
        "type": 1,
        "hint": "YYYY",
        "field_data": '{"prefix":"class-of"}',
    }
    client.call_endpoint.side_effect = [
        server(), principal(), profile_fields([existing]),
    ]

    result = mcp_module.create_custom_profile_field(
        realm_url=REALM_URL,
        name="Graduation year",
        field_type=1,
        settings={
            "hint": "YYYY",
            "field_data": {"prefix": "class-of"},
            "display_in_profile_summary": False,
            "use_for_user_matching": False,
        },
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"] == {}


def test_create_profile_field_applies_and_reads_back(client: Mock) -> None:
    created = {
        "id": 9,
        "name": "Graduation year",
        "type": 1,
        "hint": "YYYY",
    }
    client.call_endpoint.side_effect = [
        server(), principal(), profile_fields([]),
        {"result": "success", "msg": "", "id": 9},
        profile_fields([created]),
    ]

    result = mcp_module.create_custom_profile_field(
        realm_url=REALM_URL,
        name="Graduation year",
        field_type=1,
        settings={"hint": "YYYY"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == created


def test_update_profile_field_uses_expected_value(client: Mock) -> None:
    existing = {
        "id": 9,
        "name": "Graduation year",
        "type": 1,
        "hint": "YYYY",
    }
    client.call_endpoint.side_effect = [
        server(), principal(), profile_fields([existing]),
    ]

    result = mcp_module.update_custom_profile_field(
        realm_url=REALM_URL,
        field="Graduation year",
        changes={"hint": "Four digits"},
        expected={"hint": "YYYY"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/realm/profile_fields/9"
    assert result["request"] == {"hint": "Four digits"}


def test_update_profile_field_treats_omitted_boolean_as_false(client: Mock) -> None:
    existing = {
        "id": 9,
        "name": "Graduation year",
        "type": 1,
        "hint": "YYYY",
    }
    client.call_endpoint.side_effect = [
        server(), principal(), profile_fields([existing]),
    ]

    result = mcp_module.update_custom_profile_field(
        realm_url=REALM_URL,
        field="Graduation year",
        changes={"display_in_profile_summary": True},
        expected={"display_in_profile_summary": False},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"display_in_profile_summary": True}


def test_delete_profile_field_previews_values_and_reads_back(client: Mock) -> None:
    existing = {"id": 9, "name": "Graduation year", "type": 1, "hint": "YYYY"}
    client.call_endpoint.side_effect = [
        server(),
        principal(),
        profile_fields([existing]),
        {
            "result": "success",
            "msg": "",
            "members": [
                {
                    "email": "one@example.test",
                    "profile_data": {"9": {"value": "2027"}},
                },
                {
                    "email": "two@example.test",
                    "profile_data": {"9": {"value": ""}},
                },
                {
                    "email": "three@example.test",
                    "profile_data": {"9": {"value": None}},
                },
            ],
        },
        {"result": "success", "msg": ""},
        profile_fields([]),
    ]

    result = mcp_module.delete_custom_profile_field(
        realm_url=REALM_URL,
        field="Graduation year",
        expected={"field_type": 1, "hint": "YYYY"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["current"]["populated_user_count"] == 1
    assert client.call_endpoint.call_args_list[4] == call(
        url="/realm/profile_fields/9", method="DELETE", request={},
    )


def test_delete_profile_field_rejects_stale_expected_state(client: Mock) -> None:
    existing = {"id": 9, "name": "Graduation year", "type": 1, "hint": "YYYY"}
    client.call_endpoint.side_effect = [
        server(), principal(), profile_fields([existing]),
        {"result": "success", "msg": "", "members": []},
    ]

    result = mcp_module.delete_custom_profile_field(
        realm_url=REALM_URL,
        field="Graduation year",
        expected={"hint": "Four digits"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert client.call_endpoint.call_count == 4


def test_allowed_domain_requires_owner(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal(owner=False)]

    result = mcp_module.add_allowed_domain(
        realm_url=REALM_URL,
        domain="example.test",
        allow_subdomains=False,
        dry_run=True,
    ).structured_content

    assert result["status"] == "forbidden"
    assert result["error"]["code"] == "OWNER_REQUIRED"


def test_add_allowed_domain_normalizes_and_is_idempotent(client: Mock) -> None:
    existing = {"domain": "example.test", "allow_subdomains": False}
    client.call_endpoint.side_effect = [
        server(), principal(), domains([existing]),
    ]

    result = mcp_module.add_allowed_domain(
        realm_url=REALM_URL,
        domain=" Example.TEST ",
        allow_subdomains=False,
    ).structured_content

    assert result["status"] == "ok"
    assert result["resolved_mappings"]["domain"] == {
        "semantic": " Example.TEST ", "resolved": "example.test",
    }


def test_update_allowed_domain_applies_and_reads_back(client: Mock) -> None:
    before = {"domain": "example.test", "allow_subdomains": False}
    after = {"domain": "example.test", "allow_subdomains": True}
    client.call_endpoint.side_effect = [
        server(), principal(), domains([before]),
        {"result": "success", "msg": ""},
        domains([after]),
    ]

    result = mcp_module.update_allowed_domain(
        realm_url=REALM_URL,
        domain="example.test",
        allow_subdomains=True,
        expected_allow_subdomains=False,
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm/domains/example.test",
        method="PATCH",
        request={"allow_subdomains": True},
    )


def test_remove_allowed_domain_previews_dependencies_and_reads_back(client: Mock) -> None:
    before = {"domain": "example.test", "allow_subdomains": True}
    client.call_endpoint.side_effect = [
        server(),
        principal(),
        domains([before]),
        {
            "result": "success",
            "msg": "",
            "members": [
                {"email": "one@example.test"},
                {"email": "two@sub.example.test"},
                {"email": "other@elsewhere.test"},
            ],
        },
        {
            "result": "success",
            "msg": "",
            "invites": [
                {"email": "invite@example.test", "is_multiuse": False},
                {"email": None, "is_multiuse": True},
            ],
        },
        {"result": "success", "msg": ""},
        domains([]),
    ]

    result = mcp_module.remove_allowed_domain(
        realm_url=REALM_URL,
        domain="Example.TEST",
        expected_allow_subdomains=True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["current"]["matching_existing_user_count"] == 2
    assert result["current"]["matching_pending_invitation_count"] == 1
    assert client.call_endpoint.call_args_list[5] == call(
        url="/realm/domains/example.test", method="DELETE", request={},
    )


def test_remove_allowed_domain_requires_matching_expected_value(client: Mock) -> None:
    before = {"domain": "example.test", "allow_subdomains": False}
    client.call_endpoint.side_effect = [
        server(), principal(), domains([before]),
        {"result": "success", "msg": "", "members": []},
        {"result": "success", "msg": "", "invites": []},
    ]

    result = mcp_module.remove_allowed_domain(
        realm_url=REALM_URL,
        domain="example.test",
        expected_allow_subdomains=True,
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert client.call_endpoint.call_count == 5


def test_create_linkifier_dry_run_and_feature_gate(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal(), linkifiers([])]

    result = mcp_module.create_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        url_template="https://tracker.example/T-{id}",
        reverse={
            "example_input": "https://tracker.example/T-12",
            "reverse_template": "T-{id}",
        },
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"]["reverse_template"] == "T-{id}"

    client.reset_mock()
    client.call_endpoint.side_effect = [server(470), principal()]
    unsupported = mcp_module.create_linkifier(
        realm_url=REALM_URL,
        pattern="T-([0-9]+)",
        url_template="https://tracker.example/T-{1}",
        reverse={"example_input": "https://tracker.example/T-12"},
        dry_run=True,
    ).structured_content
    assert unsupported["status"] == "unsupported"


def test_create_linkifier_is_idempotent_by_pattern(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://tracker.example/T-{id}",
    }
    client.call_endpoint.side_effect = [
        server(), principal(), linkifiers([existing]),
    ]

    result = mcp_module.create_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        url_template="https://tracker.example/T-{id}",
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"] == {}


def test_update_linkifier_sends_required_pair(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://old.example/T-{id}",
    }
    updated = {**existing, "url_template": "https://new.example/T-{id}"}
    client.call_endpoint.side_effect = [
        server(), principal(), linkifiers([existing]),
        {"result": "success", "msg": ""},
        linkifiers([updated]),
    ]

    result = mcp_module.update_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        changes={"url_template": "https://new.example/T-{id}"},
        expected={"url_template": "https://old.example/T-{id}"},
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm/filters/3",
        method="PATCH",
        request={
            "pattern": r"T-(?P<id>[0-9]+)",
            "url_template": "https://new.example/T-{id}",
        },
    )


def test_update_linkifier_normalizes_reverse_field_clear(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://tracker.example/T-{id}",
        "reverse_template": None,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), linkifiers([existing]),
    ]

    result = mcp_module.update_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        changes={"reverse_template": ""},
        expected={"reverse_template": None},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {}
    assert result["changed_fields"] == []


def test_update_linkifier_verifies_clear_returned_as_null(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://tracker.example/T-{id}",
        "reverse_template": "T-{id}",
    }
    updated = {**existing, "reverse_template": None}
    client.call_endpoint.side_effect = [
        server(), principal(), linkifiers([existing]),
        {"result": "success", "msg": ""},
        linkifiers([updated]),
    ]

    result = mcp_module.update_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        changes={"reverse_template": ""},
        expected={"reverse_template": "T-{id}"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"reverse_template": None}


def test_remove_linkifier_checks_template_and_reads_back(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://tracker.example/T-{id}",
    }
    client.call_endpoint.side_effect = [
        server(), principal(), linkifiers([existing]),
        {"result": "success", "msg": ""}, linkifiers([]),
    ]

    result = mcp_module.remove_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        expected_url_template="https://tracker.example/T-{id}",
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm/filters/3", method="DELETE", request={},
    )


def test_remove_linkifier_rejects_stale_template(client: Mock) -> None:
    existing = {
        "id": 3,
        "pattern": r"T-(?P<id>[0-9]+)",
        "url_template": "https://tracker.example/T-{id}",
    }
    client.call_endpoint.side_effect = [server(), principal(), linkifiers([existing])]

    result = mcp_module.remove_linkifier(
        realm_url=REALM_URL,
        pattern=r"T-(?P<id>[0-9]+)",
        expected_url_template="https://stale.example/T-{id}",
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert client.call_endpoint.call_count == 3
