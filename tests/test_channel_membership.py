import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import APIError, MutationResult, MutationStatus, ZulipAPIError

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"
USER_IDS = {
    "admin@example.test": 1,
    "alice@example.test": 7,
    "bob@example.test": 8,
}


def state(
    users: list[str],
    *,
    current: set[int] | None = None,
    private: bool = False,
    principal_id: int = 1,
) -> tuple[object, object, int, list[int], set[int], dict[str, object], None]:
    resolved = [USER_IDS[user] for user in users]
    return (
        {
            "stream_id": 12, "name": "course", "invite_only": private,
            "is_web_public": False,
        },
        {"user_id": principal_id, "is_admin": True},
        12,
        resolved,
        set(current if current is not None else {7}),
        {
            "channel": {"semantic": "course", "resolved": 12},
            "users": [
                {"semantic": user, "resolved": user_id}
                for user, user_id in zip(users, resolved)
            ],
        },
        None,
    )


@pytest.fixture(autouse=True)
def authorize() -> None:
    core.set_admin_writes_enabled(True)
    yield
    core.set_admin_writes_enabled(False)


def test_unsubscribe_dry_run_resolves_semantic_user_without_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.unsubscribe_users_from_channel(
        REALM_URL, "course", ["alice@example.test"], dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "subscriptions": ["course"], "principals": [7],
    }
    assert result["resolved_mappings"]["users"] == [
        {"semantic": "alice@example.test", "resolved": 7},
    ]
    mutation.assert_not_called()


def test_unsubscribe_applies_and_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )
    mutation = Mock(return_value={"removed": ["course"]})
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)
    monkeypatch.setattr(
        core, "get_channel_subscribers_configuration",
        lambda stream_id: {"subscribers": []},
    )

    result = mcp_module.unsubscribe_users_from_channel(
        REALM_URL, "course", ["alice@example.test"],
    ).structured_content

    assert result["status"] == "ok"
    mutation.assert_called_once_with(
        "/users/me/subscriptions", "DELETE",
        {"subscriptions": ["course"], "principals": [7]},
    )


def test_exact_membership_dry_run_reports_additive_and_subtractive_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_channel_members(
        REALM_URL, "course", ["bob@example.test"], dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"to_subscribe": [8], "to_unsubscribe": [7]}
    mutation.assert_not_called()


def test_exact_membership_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )

    result = mcp_module.set_channel_members(
        REALM_URL, "course", ["alice@example.test"],
    ).structured_content

    assert result["status"] == "ok"
    assert "already had" in result["warnings"][0]


def test_exact_membership_expected_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )

    result = mcp_module.set_channel_members(
        REALM_URL,
        "course",
        ["bob@example.test"],
        expected_users=["bob@example.test"],
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"


def test_incomplete_membership_visibility_fails_before_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = MutationResult(
        MutationStatus.ERROR, "/users/me/subscriptions", dry_run=False,
        error=APIError(
            message="Channel subscriber inventory was absent or null",
            code="SEMANTIC_RESOLUTION_ERROR",
        ),
    )
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: (
            None, None, None, [], set(), {}, failure,
        ),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_channel_members(
        REALM_URL, "course", [],
    ).structured_content

    assert result["status"] == "error"
    mutation.assert_not_called()


def test_exact_membership_adds_before_removing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)
    monkeypatch.setattr(
        core, "get_channel_subscribers_configuration",
        lambda stream_id: {"subscribers": [8]},
    )

    result = mcp_module.set_channel_members(
        REALM_URL, "course", ["bob@example.test"],
    ).structured_content

    assert result["status"] == "ok"
    assert mutation.call_args_list == [
        call("/users/me/subscriptions", "POST", {
            "subscriptions": [{"name": "course"}], "principals": [8],
            "authorization_errors_fatal": True,
        }),
        call("/users/me/subscriptions", "DELETE", {
            "subscriptions": ["course"], "principals": [7],
        }),
    ]


def test_private_channel_protects_authenticated_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(
            users, current={1, 7}, private=True,
        ),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_channel_members(
        REALM_URL, "course", ["alice@example.test"],
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "PRIVATE_CHANNEL_ADMIN_PROTECTED"
    mutation.assert_not_called()


def test_multi_step_failure_reports_completed_and_remaining_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "_channel_membership_state",
        lambda realm, channel, users, dry: state(users),
    )
    mutation = Mock(side_effect=[{}, ZulipAPIError(APIError(
        message="removal denied", code="FORBIDDEN",
    ))])
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_channel_members(
        REALM_URL, "course", ["bob@example.test"],
    ).structured_content

    assert result["status"] == "partial"
    assert result["response"] == {
        "subscribed": [8],
        "unsubscribed": [],
        "remaining_subscriptions": [],
        "remaining_unsubscriptions": [7],
    }
