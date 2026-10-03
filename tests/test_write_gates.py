import importlib
from collections.abc import Callable
from unittest.mock import Mock

import pytest
from fastmcp.tools.tool import ToolResult

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(False)
    core.set_user_content_writes_enabled(False)
    mcp_module._session.reset()
    monkeypatch.setattr(core, "is_stream_private", lambda stream: False)
    monkeypatch.setattr(core, "is_stream_write_allowed", lambda stream: True)
    yield
    core.set_configuration_writes_enabled(False)
    core.set_user_content_writes_enabled(False)
    mcp_module._session.reset()


def assert_user_content_disabled(result: object) -> None:
    assert isinstance(result, ToolResult)
    assert result.structured_content["status"] == "disabled"
    assert result.structured_content["gate"] == "user_content"
    assert result.structured_content["error"]["code"] == (
        "USER_CONTENT_WRITES_DISABLED"
    )
    assert result.structured_content["error"]["message"].startswith(
        "User content writes are disabled; call enable_user_content_writes"
    )


@pytest.mark.parametrize(
    ("core_name", "invoke"),
    [
        ("send_message", lambda: mcp_module.send_message("general", "topic", "hi")),
        (
            "send_direct_message",
            lambda: mcp_module.send_direct_message(["person@example.com"], "hi"),
        ),
        ("add_reaction", lambda: mcp_module.add_reaction(1, "thumbs_up")),
        ("remove_reaction", lambda: mcp_module.remove_reaction(1, "thumbs_up")),
        ("edit_message", lambda: mcp_module.edit_message(1, "revised")),
        ("move_messages", lambda: mcp_module.move_messages(1, "destination")),
        ("move_messages", lambda: mcp_module.resolve_topic(1, "resolved")),
        ("upload_file", lambda: mcp_module.upload_file("/does/not/matter")),
    ],
)
def test_user_content_tools_are_blocked_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    core_name: str,
    invoke: Callable[[], object],
) -> None:
    mutation = Mock()
    monkeypatch.setattr(core, core_name, mutation)

    result = invoke()

    assert_user_content_disabled(result)
    mutation.assert_not_called()


def test_reply_is_blocked_before_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    mutation = Mock()
    monkeypatch.setattr(core, "send_message", mutation)
    mcp_module._session.active = True
    mcp_module._session.stream = "general"
    mcp_module._session.topic = "topic"

    result = mcp_module.reply("hello")

    assert_user_content_disabled(result)
    mutation.assert_not_called()


def test_end_session_blocks_farewell_but_completes_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = Mock()
    monkeypatch.setattr(core, "send_message", mutation)
    mcp_module._session.active = True
    mcp_module._session.stream = "general"
    mcp_module._session.topic = "topic"

    result = mcp_module.end_session()

    assert_user_content_disabled(result)
    assert not mcp_module._session.active
    mutation.assert_not_called()


def test_silent_end_session_does_not_require_user_content_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = Mock()
    monkeypatch.setattr(core, "send_message", mutation)
    mcp_module._session.active = True
    mcp_module._session.stream = "general"
    mcp_module._session.topic = "topic"

    result = mcp_module.end_session("")

    assert result == "Session ended. Was chatting in #general > topic"
    assert not mcp_module._session.active
    mutation.assert_not_called()


def test_enabled_user_content_gate_allows_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation = Mock(return_value={"result": "success", "id": 7})
    monkeypatch.setattr(core, "send_message", mutation)
    core.set_user_content_writes_enabled(True)

    result = mcp_module.send_message("general", "topic", "hello")

    assert result == "Message sent to #general > topic (id: 7)"
    mutation.assert_called_once()
    assert not core.configuration_writes_enabled()


def test_automatic_channel_subscription_uses_configuration_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    monkeypatch.setattr(core, "get_client", lambda: client)
    monkeypatch.setattr(core, "is_bot_subscribed", lambda stream: False)
    monkeypatch.setattr(core, "is_stream_private", lambda stream: False)

    assert not core.ensure_subscribed("general")
    client.add_subscriptions.assert_not_called()

    core.set_configuration_writes_enabled(True)
    client.add_subscriptions.return_value = {"result": "success"}
    assert core.ensure_subscribed("general")
    client.add_subscriptions.assert_called_once_with(
        streams=[{"name": "general"}],
    )


def test_revocation_during_validation_blocks_the_external_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    monkeypatch.setattr(core, "get_client", lambda: client)

    def revoke_during_read(message_id: int) -> tuple[dict[str, object], None]:
        core.set_user_content_writes_enabled(False)
        return {"id": message_id}, None

    monkeypatch.setattr(core, "_get_stream_message_for_write", revoke_during_read)
    core.set_user_content_writes_enabled(True)

    result = mcp_module.edit_message(1, "revised")

    assert_user_content_disabled(result)
    client.update_message.assert_not_called()


def test_reenable_after_core_rejection_preserves_disabled_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_after_reenable(message_id: int, content: str) -> dict:
        core.set_user_content_writes_enabled(True)
        raise core.ZulipAPIError(core.APIError(
            message="disabled during validation",
            code="USER_CONTENT_WRITES_DISABLED",
        ))

    monkeypatch.setattr(core, "edit_message", reject_after_reenable)
    core.set_user_content_writes_enabled(True)

    result = mcp_module.edit_message(1, "revised")

    assert_user_content_disabled(result)
