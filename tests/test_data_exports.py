import asyncio
import importlib
import json
from unittest.mock import Mock

import pytest

from zulipmcp import core
from zulipmcp.configuration import REDACTED

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"
EXPORT_URL = "https://realm.example.test/exports/bearer-secret.tar.gz"


def export_job(
    *,
    export_id: int = 7,
    export_type: object = "public",
    pending: bool = False,
    export_time: float = 1000,
    export_url: object = EXPORT_URL,
    failed_timestamp: object = None,
    deleted_timestamp: object = None,
    export_from_prior_server: bool = False,
) -> dict[str, object]:
    return {
        "id": export_id,
        "export_type": export_type,
        "pending": pending,
        "export_time": export_time,
        "export_url": export_url,
        "failed_timestamp": failed_timestamp,
        "deleted_timestamp": deleted_timestamp,
        "export_from_prior_server": export_from_prior_server,
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(True)
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {
                "realm_url": REALM_URL,
                "zulip_feature_level": 500,
            },
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    yield
    core.set_configuration_writes_enabled(False)


def test_get_data_exports_redacts_download_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_data_exports_configuration",
        lambda: {"exports": [export_job()]},
    )

    result = mcp_module.get_data_exports().structured_content

    assert result["status"] == "ok"
    assert result["data"]["exports"][0]["export_url"] == REDACTED
    assert "bearer-secret" not in json.dumps(result)


def test_fastmcp_dispatches_export_audit_and_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_data_exports_configuration",
        lambda: {"exports": [export_job()]},
    )

    audit = asyncio.run(mcp_module.mcp.call_tool("get_data_exports", {}))
    planned = asyncio.run(mcp_module.mcp.call_tool(
        "create_data_export",
        {
            "realm_url": REALM_URL,
            "export_type": "public",
            "dry_run": True,
        },
    ))

    assert audit.content[0].text == "Data exports: ok."
    assert audit.structured_content["data"]["exports"][0]["export_url"] == (
        REDACTED
    )
    assert planned.content[0].text == "Data export creation: dry_run."
    assert planned.structured_content["request"] == {"export_type": "public"}
    assert "bearer-secret" not in json.dumps(audit.structured_content)


@pytest.mark.parametrize(
    ("feature_level", "export_type", "wire_type"),
    [
        (303, "public", None),
        (304, "full_with_consent", 2),
        (449, "full_without_consent", "full_without_consent"),
    ],
)
def test_create_data_export_dry_run_translates_feature_levels(
    monkeypatch: pytest.MonkeyPatch,
    feature_level: int,
    export_type: str,
    wire_type: object,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": feature_level},
            {"is_owner": True, "is_admin": True},
            None,
        ),
    )
    monkeypatch.setattr(
        core, "get_data_exports_configuration", lambda: {"exports": []},
    )
    monkeypatch.setattr(
        core,
        "_read_write_state",
        lambda fields, defaults: ({"owner_full_content_access": True}, []),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.create_data_export(
        REALM_URL, export_type, dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == (
        {} if wire_type is None else {"export_type": wire_type}
    )
    assert result["resolved_mappings"]["export_type"] == {
        "semantic": export_type,
        "resolved": wire_type,
    }
    assert f"CREATE {export_type} EXPORT" in result["warnings"][-1]
    mutation.assert_not_called()


def test_create_data_export_rejects_unsupported_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 303},
            {"is_admin": True},
            None,
        ),
    )

    full = mcp_module.create_data_export(
        REALM_URL, "full_with_consent", dry_run=True,
    ).structured_content
    invalid = mcp_module.create_data_export(
        REALM_URL, "unknown", dry_run=True,
    ).structured_content

    assert full["status"] == "unsupported"
    assert full["error"]["code"] == "UNSUPPORTED_FEATURE"
    assert invalid["status"] == "error"
    assert invalid["error"]["code"] == "INVALID_EXPORT_TYPE"


def test_full_without_consent_requires_owner_and_realm_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 500},
            {"is_owner": False, "is_admin": True},
            None,
        ),
    )
    not_owner = mcp_module.create_data_export(
        REALM_URL, "full_without_consent", dry_run=True,
    ).structured_content
    assert not_owner["status"] == "forbidden"
    assert not_owner["error"]["code"] == "OWNER_REQUIRED"

    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 500},
            {"is_owner": True, "is_admin": True},
            None,
        ),
    )
    monkeypatch.setattr(
        core,
        "_read_write_state",
        lambda fields, defaults: ({"owner_full_content_access": False}, []),
    )
    disabled = mcp_module.create_data_export(
        REALM_URL, "full_without_consent", dry_run=True,
    ).structured_content
    assert disabled["status"] == "forbidden"
    assert disabled["error"]["code"] == (
        "OWNER_FULL_CONTENT_ACCESS_REQUIRED"
    )


def test_create_data_export_requires_confirmation_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([
        {"exports": []},
        {"exports": []},
        {"exports": [export_job(pending=True)]},
    ])
    monkeypatch.setattr(
        core, "get_data_exports_configuration", lambda: next(inventories),
    )
    mutation = Mock(return_value={"id": 7})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    denied = mcp_module.create_data_export(
        REALM_URL, confirmation="yes",
    ).structured_content
    assert denied["status"] == "conflict"
    assert denied["error"]["code"] == "CONFIRMATION_REQUIRED"
    mutation.assert_not_called()

    result = mcp_module.create_data_export(
        REALM_URL, confirmation="CREATE public EXPORT",
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"]["export_url"] == REDACTED
    assert "bearer-secret" not in json.dumps(result)
    mutation.assert_called_once_with(
        "/export/realm", "POST", {"export_type": "public"},
    )


def test_create_data_export_missing_id_is_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_data_exports_configuration", lambda: {"exports": []},
    )
    monkeypatch.setattr(
        core, "configuration_mutation_request", lambda *args: {},
    )

    result = mcp_module.create_data_export(
        REALM_URL, confirmation="CREATE public EXPORT",
    ).structured_content

    assert result["status"] == "partial"
    assert "omitted its ID" in result["warnings"][-1]


def test_delete_data_export_dry_run_requires_exact_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_data_exports_configuration",
        lambda: {"exports": [export_job()]},
    )

    stale = mcp_module.delete_data_export(
        REALM_URL, 7, 999, dry_run=True,
    ).structured_content
    valid = mcp_module.delete_data_export(
        REALM_URL, 7, 1000, dry_run=True,
    ).structured_content

    assert stale["status"] == "conflict"
    assert stale["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    assert valid["status"] == "dry_run"
    assert valid["current"]["export_url"] == REDACTED
    assert "DELETE EXPORT 7" in valid["warnings"][-1]
    assert "bearer-secret" not in json.dumps(valid)


@pytest.mark.parametrize(
    "overrides",
    [
        {"pending": True},
        {"failed_timestamp": 1010},
        {"deleted_timestamp": 1020},
        {"export_from_prior_server": True},
        {"export_url": None},
    ],
)
def test_delete_data_export_rejects_unavailable_archives(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    monkeypatch.setattr(
        core,
        "get_data_exports_configuration",
        lambda: {"exports": [export_job(**overrides)]},
    )

    result = mcp_module.delete_data_export(
        REALM_URL, 7, 1000, dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPORT_NOT_DELETABLE"


def test_delete_data_export_requires_confirmation_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter([
        {"exports": [export_job()]},
        {"exports": [export_job()]},
        {"exports": [export_job(deleted_timestamp=1100)]},
    ])
    monkeypatch.setattr(
        core, "get_data_exports_configuration", lambda: next(inventories),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    denied = mcp_module.delete_data_export(
        REALM_URL, 7, 1000, confirmation="DELETE 7",
    ).structured_content
    assert denied["status"] == "conflict"
    assert denied["error"]["code"] == "CONFIRMATION_REQUIRED"
    mutation.assert_not_called()

    result = mcp_module.delete_data_export(
        REALM_URL, 7, 1000, confirmation="DELETE EXPORT 7",
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"]["deleted_timestamp"] == 1100
    assert "bearer-secret" not in json.dumps(result)
    mutation.assert_called_once_with("/export/realm/7", "DELETE", {})


def test_delete_data_export_validates_inputs_before_network() -> None:
    invalid_id = core.delete_data_export(REALM_URL, 0, 1000, dry_run=True)
    invalid_time = core.delete_data_export(REALM_URL, 7, 0, dry_run=True)

    assert invalid_id.error is not None
    assert invalid_id.error.code == "INVALID_EXPORT_ID"
    assert invalid_time.error is not None
    assert invalid_time.error.code == "INVALID_EXPORT_TIME"
