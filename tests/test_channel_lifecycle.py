import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import APIError, MutationResult, MutationStatus

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"


def channel(*, archived: bool = False, default: bool = False) -> dict[str, object]:
    return {
        "stream_id": 12,
        "name": "temporary test",
        "description": "Lifecycle test",
        "invite_only": False,
        "is_web_public": False,
        "is_archived": archived,
        "is_default": default,
        "first_message_id": 100,
        "message_retention_days": None,
        "topics_policy": 1,
        "folder_id": None,
    }


def impact(*, referenced: bool = False) -> tuple[dict[str, object], list[str]]:
    return ({
        "channel": channel(),
        "privacy": "public",
        "messages_retained": True,
        "message_history_present": True,
        "subscriber_count": 1,
        "visible_subscribers": [{
            "user_id": 1, "email": "owner@example.test", "full_name": "Owner",
        }],
        "topic_count": 1,
        "realm_references": {"signup_announcements_stream_id": 12} if referenced else {},
        "realm_references_complete": True,
        "channel_folder_membership": [],
    }, ["Archival retains all channel messages and hides them with the channel"])


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(True)
    monkeypatch.setattr(
        core, "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 500},
            {"user_id": 1, "is_admin": True},
            None,
        ),
    )
    monkeypatch.setattr(core, "_channel_impact", lambda item: impact())
    yield
    core.set_configuration_writes_enabled(False)


def test_archive_dry_run_resolves_archived_inventory_and_retains_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_channel_inventory", lambda: [channel()])
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True, expected_archived=False, dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/streams/12"
    assert result["current"]["impact"]["messages_retained"] is True
    assert result["current"]["impact"]["message_history_present"] is True
    assert "permanently deleted" not in str(result).lower()
    mutation.assert_not_called()


@pytest.mark.parametrize("name", ["12", "unknown"])
def test_archive_rejects_numeric_and_unknown_names(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    monkeypatch.setattr(core, "_channel_inventory", lambda: [channel()])

    result = mcp_module.set_channel_archived(
        REALM_URL, name, True, dry_run=True,
    ).structured_content

    assert result["status"] in {"conflict", "error"}
    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_archive_rejects_ambiguous_active_and_archived_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_channel_inventory", lambda: [channel(), channel(archived=True)],
    )

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True, dry_run=True,
    ).structured_content

    assert result["error"]["code"] == "SEMANTIC_RESOLUTION_ERROR"


def test_archive_expected_state_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_channel_inventory", lambda: [channel()])

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True, expected_archived=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"


@pytest.mark.parametrize("default,referenced", [(True, False), (False, True)])
def test_archive_blocks_default_and_referenced_channels(
    monkeypatch: pytest.MonkeyPatch, default: bool, referenced: bool,
) -> None:
    monkeypatch.setattr(core, "_channel_inventory", lambda: [channel(default=default)])
    monkeypatch.setattr(core, "_channel_impact", lambda item: impact(referenced=referenced))

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True, dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "CHANNEL_ARCHIVE_BLOCKED"


def test_archive_uses_delete_and_confirms_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_inventory",
        Mock(side_effect=[[channel()], [channel(archived=True)]]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True,
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"is_archived": True}
    mutation.assert_called_once_with("/streams/12", method="DELETE", request={})


def test_already_archived_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_channel_inventory", lambda: [channel(archived=True)])
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True,
    ).structured_content

    assert result["status"] == "ok"
    assert "already had" in result["warnings"][-1]
    mutation.assert_not_called()


def test_unarchive_uses_patch_and_confirms_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_inventory",
        Mock(side_effect=[[channel(archived=True)], [channel()]]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", False, expected_archived=True,
    ).structured_content

    assert result["status"] == "ok"
    mutation.assert_called_once_with(
        "/streams/12", method="PATCH", request={"is_archived": False},
    )


def test_mismatched_archive_readback_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_inventory", Mock(side_effect=[[channel()], [channel()]]),
    )
    monkeypatch.setattr(core, "configuration_mutation_request", Mock(return_value={}))

    result = mcp_module.set_channel_archived(
        REALM_URL, "temporary test", True,
    ).structured_content

    assert result["status"] == "partial"


def test_destination_mismatch_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    mismatch = MutationResult(
        MutationStatus.CONFLICT, "/streams/{stream_id}", dry_run=True,
        error=APIError(
            message="Explicit destination realm does not match this MCP server",
            code="DESTINATION_REALM_MISMATCH",
        ),
    )
    monkeypatch.setattr(
        core, "_admin_destination",
        lambda endpoint, realm_url, dry_run: (None, None, mismatch),
    )

    result = mcp_module.set_channel_archived(
        "https://wrong.example.test", "temporary test", True, dry_run=True,
    ).structured_content

    assert result["error"]["code"] == "DESTINATION_REALM_MISMATCH"


def test_archive_alias_routes_to_convergent_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implementation = Mock(return_value=MutationResult(
        MutationStatus.DRY_RUN, "/streams/12", dry_run=True,
        desired={"is_archived": True},
    ))
    monkeypatch.setattr(core, "set_channel_archived", implementation)

    result = mcp_module.archive_channel(
        REALM_URL, "temporary test", expected_archived=False, dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    implementation.assert_called_once_with(
        REALM_URL, "temporary test", True, False, True,
    )
