"""Launch one Zulip-backed coding agent without running the listener."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .agent_backends import build_agent_cmd

DEFAULT_SYSTEM_PROMPT = Path(__file__).with_name("default_system_prompt.md")


@dataclass
class DirectLaunchConfig:
    """Configuration consumed by the shared backend command builders."""

    backend: str
    agent_command: str
    working_dir: Path
    system_prompt: Path = DEFAULT_SYSTEM_PROMPT
    mcp_config: Path = Path(".zulipmcp-global-mcp")
    strict_mcp_config: bool = False
    zuliprc: Path = Path(".zuliprc")
    codex_permission_mode: str = "parity"
    listen_timeout_hours: float = 2
    backend_flags: list[str] = field(default_factory=list)
    opencode_model: str = ""
    opencode_agent: str = ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch one coding agent that converses through ZulipMCP.",
    )
    parser.add_argument("--backend", choices=["claude", "codex"], required=True)
    parser.add_argument("--stream", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--working-dir", required=True)
    parser.add_argument("--agent-command")
    parser.add_argument("--zuliprc")
    parser.add_argument("--mcp-config")
    parser.add_argument("--system-prompt", default=str(DEFAULT_SYSTEM_PROMPT))
    parser.add_argument("--trigger-message-id")
    parser.add_argument("--session-user-email")
    parser.add_argument("--listen-timeout-hours", type=float, default=2)
    parser.add_argument(
        "--codex-permission-mode",
        choices=["parity", "workspace-write", "read-only", "none"],
        default="parity",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "backend_flags",
        nargs=argparse.REMAINDER,
        help="Additional backend flags after --",
    )
    return parser


def _build_launch(
    args: argparse.Namespace,
) -> tuple[DirectLaunchConfig, dict[str, str], list[str]]:
    working_dir = Path(args.working_dir).expanduser().resolve()
    if not working_dir.is_dir():
        raise FileNotFoundError(f"Working directory not found: {working_dir}")

    system_prompt = Path(args.system_prompt).expanduser().resolve()
    if not system_prompt.is_file():
        raise FileNotFoundError(f"System prompt not found: {system_prompt}")

    mcp_config = (
        Path(args.mcp_config).expanduser().resolve()
        if args.mcp_config
        else working_dir / ".zulipmcp-global-mcp"
    )
    if args.mcp_config and not mcp_config.is_file():
        raise FileNotFoundError(f"MCP config not found: {mcp_config}")

    zuliprc = (
        Path(args.zuliprc).expanduser().resolve()
        if args.zuliprc
        else working_dir / ".zuliprc"
    )
    if args.zuliprc and not zuliprc.is_file():
        raise FileNotFoundError(f"Zulip config not found: {zuliprc}")

    flags = list(args.backend_flags or [])
    if flags[:1] == ["--"]:
        flags = flags[1:]
    cfg = DirectLaunchConfig(
        backend=args.backend,
        agent_command=args.agent_command or args.backend,
        working_dir=working_dir,
        system_prompt=system_prompt,
        mcp_config=mcp_config,
        strict_mcp_config=bool(args.mcp_config),
        zuliprc=zuliprc,
        codex_permission_mode=args.codex_permission_mode,
        listen_timeout_hours=max(args.listen_timeout_hours, 0.01),
        backend_flags=flags,
    )

    env = os.environ.copy()
    if args.zuliprc:
        env["ZULIP_RC_PATH"] = str(zuliprc)
    if args.trigger_message_id:
        env["TRIGGER_MESSAGE_ID"] = str(args.trigger_message_id)
    if args.session_user_email:
        env["SESSION_USER_EMAIL"] = str(args.session_user_email)
    if args.backend == "claude":
        env["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "10800000"

    return cfg, env, build_agent_cmd(cfg, args.stream, args.topic, env)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    cfg, env, command = _build_launch(args)
    if args.dry_run:
        print(json.dumps(command))
        return
    os.chdir(cfg.working_dir)
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
