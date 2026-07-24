"""Tests for direct MCP-only coding-agent launches."""

from __future__ import annotations

import json
from pathlib import Path

from zulipmcp import launch_agent


def _args(tmp_path: Path, *extra: str):
    return launch_agent._parser().parse_args(
        [
            "--backend",
            "claude",
            "--stream",
            "engineering",
            "--topic",
            "large task",
            "--working-dir",
            str(tmp_path),
            *extra,
        ]
    )


def test_claude_launch_uses_global_mcp_and_zulip_session_prompt(
    tmp_path: Path,
) -> None:
    args = _args(
        tmp_path,
        "--agent-command",
        "/opt/claude",
        "--trigger-message-id",
        "42",
        "--session-user-email",
        "john@example.com",
    )

    cfg, env, command = launch_agent._build_launch(args)

    assert cfg.mcp_config == tmp_path / ".zulipmcp-global-mcp"
    assert "--mcp-config" not in command
    assert command[0] == "/opt/claude"
    assert command[-2:] == [
        "-p",
        (
            "Call set_context('engineering', 'large task') to begin, "
            "then handle the request and listen for follow-ups. Whenever yielding, "
            "call listen(timeout_hours=2). A timeout alone is not a reason to exit: "
            "send a contextual check-in and listen again. Continue until dismissed "
            "or explicitly told to end."
        ),
    ]
    assert env["TRIGGER_MESSAGE_ID"] == "42"
    assert env["SESSION_USER_EMAIL"] == "john@example.com"
    assert env["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "10800000"


def test_codex_launch_uses_exec_mode_and_global_mcp(tmp_path: Path) -> None:
    args = launch_agent._parser().parse_args(
        [
            "--backend",
            "codex",
            "--stream",
            "engineering",
            "--topic",
            "large task",
            "--working-dir",
            str(tmp_path),
            "--agent-command",
            "/opt/codex",
        ]
    )

    _cfg, _env, command = launch_agent._build_launch(args)

    assert command[:3] == ["/opt/codex", "--search", "exec"]
    assert "--mcp-config" not in command
    assert "listener" not in " ".join(command)
    assert command[-1].startswith("Call set_context('engineering', 'large task')")


def test_explicit_claude_mcp_config_is_strict(tmp_path: Path) -> None:
    mcp_config = tmp_path / "claude.mcp.json"
    mcp_config.write_text('{"mcpServers": {}}')
    args = _args(tmp_path, "--mcp-config", str(mcp_config))

    _cfg, _env, command = launch_agent._build_launch(args)

    assert command[command.index("--mcp-config") + 1] == str(mcp_config)
    assert "--strict-mcp-config" in command


def test_dry_run_prints_command_without_launching(
    tmp_path: Path,
    capsys,
) -> None:
    launch_agent.main(
        [
            "--backend",
            "claude",
            "--stream",
            "engineering",
            "--topic",
            "large task",
            "--working-dir",
            str(tmp_path),
            "--dry-run",
        ]
    )

    command = json.loads(capsys.readouterr().out)
    assert command[0] == "claude"
    assert command[-1].startswith("Call set_context('engineering', 'large task')")
