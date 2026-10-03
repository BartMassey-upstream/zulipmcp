import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"


def human(
    *,
    user_id: int = 2,
    full_name: str = "Course Staff",
    role: int = 400,
    active: bool = True,
) -> dict[str, object]:
    return {
        "user_id": user_id,
        "email": "staff@example.test",
        "full_name": full_name,
        "role": role,
        "is_active": active,
        "is_bot": False,
        "profile_data": {"9": {"value": "old"}},
    }


def bot(*, active: bool = True) -> dict[str, object]:
    return {
        "user_id": 3,
        "email": "helper-bot@example.test",
        "full_name": "Helper",
        "is_active": active,
        "is_bot": True,
        "bot_owner_id": 2,
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(True)
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL},
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    yield
    core.set_configuration_writes_enabled(False)


def test_update_user_dry_run_resolves_role_and_profile_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )
    monkeypatch.setattr(
        core,
        "get_profile_fields_configuration",
        lambda: {"custom_fields": [{"id": 9, "name": "Cohort"}]},
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.update_user_configuration(
        REALM_URL,
        "staff@example.test",
        {
            "full_name": "Teaching Staff",
            "role": "administrator",
            "profile_values": {"Cohort": "new"},
        },
        expected={"role": "member", "profile_values": {"Cohort": "old"}},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/users/2"
    assert result["request"] == {
        "full_name": "Teaching Staff",
        "role": 200,
        "profile_data": [{"id": 9, "value": "new"}],
    }
    assert result["resolved_mappings"]["user"]["resolved"] == 2
    mutation.assert_not_called()


def test_update_user_rejects_numeric_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )

    result = mcp_module.update_user_configuration(
        REALM_URL, "staff@example.test", {"role": 200}, dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_update_user_accepts_empty_profile_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )

    result = mcp_module.update_user_configuration(
        REALM_URL, "staff@example.test", {"profile_values": {}}, dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {}


def test_update_user_rejects_users_type_profile_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )
    monkeypatch.setattr(
        core,
        "get_profile_fields_configuration",
        lambda: {"custom_fields": [{"id": 9, "name": "Mentors", "type": 6}]},
    )

    result = mcp_module.update_user_configuration(
        REALM_URL,
        "staff@example.test",
        {"profile_values": {"Mentors": [1]}},
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert "Users-type" in result["error"]["message"]


def test_update_user_gate_rejection_is_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )
    core.set_configuration_writes_enabled(False)

    result = mcp_module.update_user_configuration(
        REALM_URL, "staff@example.test", {"full_name": "Teaching Staff"},
    ).structured_content

    assert result["status"] == "disabled"
    assert result["error"]["code"] == "CONFIGURATION_WRITES_DISABLED"


def test_update_user_requires_owner_for_owner_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL},
            {"user_id": 1, "is_owner": False, "is_admin": True},
            None,
        ),
    )
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )

    result = mcp_module.update_user_configuration(
        REALM_URL, "staff@example.test", {"role": "owner"}, dry_run=True,
    ).structured_content

    assert result["status"] == "forbidden"
    assert result["error"]["code"] == "OWNER_REQUIRED"


def test_update_user_applies_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = human()
    after = human(full_name="Teaching Staff", role=200)
    monkeypatch.setattr(
        core,
        "get_users_configuration",
        Mock(side_effect=[{"members": [before]}, {"members": [after]}]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.update_user_configuration(
        REALM_URL,
        "Course Staff",
        {"full_name": "Teaching Staff", "role": "administrator"},
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"]["role"] == "administrator"
    mutation.assert_called_once_with(
        "/users/2", "PATCH", {"full_name": "Teaching Staff", "role": 200},
    )


def test_user_deactivation_dry_run_previews_owned_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_users_configuration",
        lambda: {"members": [human(), bot()]},
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_user_active(
        REALM_URL,
        "staff@example.test",
        False,
        expected_active=True,
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/users/2"
    assert result["request"] == {}
    assert result["resolved_mappings"]["impact"]["owned_bot_count"] == 1
    assert result["resolved_mappings"]["impact"]["content_deletion_requested"] is False
    assert "owned bots" in result["warnings"][0]
    mutation.assert_not_called()


def test_user_deactivation_blocks_self(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL},
            {"user_id": 2, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )

    result = mcp_module.set_user_active(
        REALM_URL, "staff@example.test", False, dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "SELF_DEACTIVATION_FORBIDDEN"


def test_user_deactivation_expected_conflict_makes_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_user_active(
        REALM_URL, "staff@example.test", False, expected_active=False,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    mutation.assert_not_called()


def test_user_reactivation_applies_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = human(active=False)
    after = human(active=True)
    monkeypatch.setattr(
        core,
        "get_users_configuration",
        Mock(side_effect=[{"members": [before]}, {"members": [after]}]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_user_active(
        REALM_URL, "Course Staff", True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"]["is_active"] is True
    assert mutation.call_args_list == [call("/users/2/reactivate", "POST", {})]


def test_user_lifecycle_gate_rejection_is_not_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [human()]},
    )
    core.set_configuration_writes_enabled(False)

    result = mcp_module.set_user_active(
        REALM_URL, "staff@example.test", False,
    ).structured_content

    assert result["status"] == "disabled"
    assert result["error"]["code"] == "CONFIGURATION_WRITES_DISABLED"
