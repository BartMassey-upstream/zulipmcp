import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import SectionResult, SectionStatus

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"


def owner() -> dict[str, object]:
    return {
        "user_id": 1, "email": "owner@example.test", "full_name": "Owner",
        "is_bot": False, "is_active": True,
    }


def bot_item(channels: list[str] | None = None) -> dict[str, object]:
    return {
        "user_id": 2, "email": "helper-bot@example.test", "full_name": "Helper",
        "is_bot": True, "is_active": True, "bot_type": 1, "bot_owner_id": 1,
        "owner": {"user_id": 1, "email": "owner@example.test", "full_name": "Owner"},
        "default_sending_stream": 10, "default_sending_channel": "general",
        "default_events_register_stream": 11,
        "default_events_register_channel": "events",
        "default_all_public_streams": False,
        "subscription_status": "ok",
        "channel_subscriptions": [
            {"stream_id": index + 10, "name": name}
            for index, name in enumerate(channels or ["general"])
        ],
        "subscription_errors": [],
    }


def audit(item: dict[str, object], status: SectionStatus = SectionStatus.OK) -> SectionResult:
    return SectionResult(status, data={"bots": [item]})


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_admin_writes_enabled(True)
    monkeypatch.setattr(
        core, "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL}, {"user_id": 1}, None,
        ),
    )
    monkeypatch.setattr(
        core, "get_users_configuration",
        lambda: {"members": [owner(), bot_item()]},
    )
    monkeypatch.setattr(
        core, "get_streams_configuration",
        lambda **kwargs: {"streams": [
            {"stream_id": 10, "name": "general"},
            {"stream_id": 11, "name": "events"},
            {"stream_id": 12, "name": "new"},
        ]},
    )
    yield
    core.set_admin_writes_enabled(False)


def test_update_bot_resolves_owner_and_channels_for_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_bot_audit_item", lambda reference: (audit(bot_item()), bot_item()))

    result = mcp_module.update_bot_configuration(
        REALM_URL,
        "helper-bot@example.test",
        {"owner": "Owner", "default_sending_channel": "new"},
        expected={"full_name": "Helper"},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"default_sending_stream": 12}
    assert result["resolved_mappings"]["owner"]["resolved"] == 1


def test_update_bot_expected_conflict_makes_no_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)
    monkeypatch.setattr(core, "_bot_audit_item", lambda reference: (audit(bot_item()), bot_item()))

    result = mcp_module.update_bot_configuration(
        REALM_URL, "helper-bot@example.test", {"full_name": "New"},
        expected={"full_name": "Stale"},
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_VALUE_MISMATCH"
    mutation.assert_not_called()


def test_exact_subscriptions_add_before_remove_and_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = bot_item(["general", "events"])
    after = bot_item(["events", "new"])
    monkeypatch.setattr(
        core, "_bot_audit_item",
        Mock(side_effect=[(audit(before), before), (audit(after), after)]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_bot_channel_subscriptions(
        REALM_URL, "helper-bot@example.test", ["events", "new"],
    ).structured_content

    assert result["status"] == "ok"
    assert result["request"]["to_subscribe"] == ["new"]
    assert result["request"]["to_unsubscribe"] == ["general"]
    assert mutation.call_args_list == [
        call("/users/me/subscriptions", "POST", {
            "subscriptions": [{"name": "new"}], "principals": [2],
            "authorization_errors_fatal": True,
        }),
        call("/users/me/subscriptions", "DELETE", {
            "subscriptions": ["general"], "principals": [2],
        }),
    ]


def test_exact_subscriptions_fail_closed_without_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = bot_item()
    item["subscription_status"] = "partial"
    monkeypatch.setattr(
        core, "_bot_audit_item",
        lambda reference: (audit(item, SectionStatus.PARTIAL), item),
    )
    mutation = Mock()
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)

    result = mcp_module.set_bot_channel_subscriptions(
        REALM_URL, "helper-bot@example.test", [],
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "CHANNEL_VISIBILITY_INCOMPLETE"
    mutation.assert_not_called()


def test_create_bot_redacts_api_key_and_reads_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core, "get_users_configuration", lambda: {"members": [owner()]},
    )
    created = bot_item([])
    created.pop("default_events_register_stream")
    created.pop("default_events_register_channel")
    mutation = Mock(return_value={
        "user_id": 2, "email": "helper-bot@example.test",
        "api_key": "never-return-this",
    })
    monkeypatch.setattr(core, "administrative_mutation_request", mutation)
    monkeypatch.setattr(core, "_bot_audit_item", lambda reference: (audit(created), created))

    result = mcp_module.create_bot(
        REALM_URL, "helper", "Helper", "owner@example.test",
        default_sending_channel="general",
    ).structured_content

    assert result["status"] == "ok"
    assert "never-return-this" not in str(result)
    assert "api_key" not in result["response"]
    assert mutation.call_args.args[0:2] == ("/bots", "POST")
    assert mutation.call_args.args[2]["bot_type"] == 1


def test_create_bot_rejects_deactivated_match(monkeypatch: pytest.MonkeyPatch) -> None:
    inactive = bot_item()
    inactive["is_active"] = False
    monkeypatch.setattr(
        core, "get_users_configuration",
        lambda: {"members": [owner(), inactive]},
    )

    result = mcp_module.create_bot(
        REALM_URL, "helper", "Helper", "owner@example.test", dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "BOT_ALREADY_DEACTIVATED"
