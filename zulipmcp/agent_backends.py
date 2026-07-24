"""Backend-specific launch helpers for Zulip agent sessions."""

from __future__ import annotations

import json
import os
import re
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Protocol


class LaunchConfig(Protocol):
    """Subset of listener.Config needed to build backend commands."""

    backend: str
    agent_command: str
    zuliprc: Path
    mcp_config: Path
    system_prompt: Path
    working_dir: Path
    backend_flags: list[str]
    codex_permission_mode: str
    opencode_model: str
    opencode_agent: str


CODEX_MCP_TOOL_TIMEOUT_SEC = 10_800
OPENCODE_MCP_TIMEOUT_MS = 10_800_000

_BARE_TOML_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_CODEX_TOP_LEVEL_ONLY_FLAGS_WITHOUT_VALUE = {
    "--search",
    "--no-alt-screen",
}
_CODEX_TOP_LEVEL_ONLY_FLAGS_WITH_VALUE = {
    "--remote",
    "--remote-auth-token-env",
}


def bootstrap_prompt(
    stream: str,
    topic: str,
    *,
    listen_timeout_hours: float | None = None,
) -> str:
    """Return the initial instruction given to a newly spawned agent."""
    prompt = (
        f"Call set_context({stream!r}, {topic!r}) to begin, "
        f"then handle the request and listen for follow-ups."
    )
    if listen_timeout_hours is not None:
        prompt += (
            f" Whenever yielding, call listen(timeout_hours={listen_timeout_hours:g})."
            " A timeout alone is not a reason to exit: send a contextual check-in"
            " and listen again. Continue until dismissed or explicitly told to end."
        )
    return prompt


def _bootstrap_prompt_for_config(
    cfg: LaunchConfig,
    stream: str,
    topic: str,
) -> str:
    return bootstrap_prompt(
        stream,
        topic,
        listen_timeout_hours=getattr(cfg, "listen_timeout_hours", None),
    )


def build_agent_env(cfg: LaunchConfig, msg: dict[str, Any]) -> dict[str, str]:
    """Set environment variables that zulipmcp reads in the MCP server."""
    env = os.environ.copy()
    env["ZULIP_RC_PATH"] = str(cfg.zuliprc.resolve())
    env["TRIGGER_MESSAGE_ID"] = str(msg["id"])
    env["SESSION_USER_EMAIL"] = msg.get("sender_email", "")
    if cfg.backend == "claude":
        env["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = "10800000"  # 3 hours
    elif cfg.backend == "opencode":
        env["OPENCODE_CONFIG_CONTENT"] = _opencode_config_json(cfg, env)
    return env


def build_agent_cmd(
    cfg: LaunchConfig,
    stream: str,
    topic: str,
    env: MutableMapping[str, str] | None = None,
) -> list[str]:
    """Assemble the configured backend CLI invocation."""
    if cfg.backend == "claude":
        return _build_claude_cmd(cfg, stream, topic)
    if cfg.backend == "codex":
        if env is None:
            env = os.environ.copy()
        return _build_codex_cmd(cfg, stream, topic, env)
    if cfg.backend == "opencode":
        return _build_opencode_cmd(cfg, stream, topic)
    raise ValueError(f"Unsupported backend: {cfg.backend!r}")


def _build_claude_cmd(cfg: LaunchConfig, stream: str, topic: str) -> list[str]:
    cmd = [
        cfg.agent_command,
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if cfg.mcp_config.exists():
        cmd += ["--mcp-config", str(cfg.mcp_config.resolve())]
        if getattr(cfg, "strict_mcp_config", False):
            cmd.append("--strict-mcp-config")
    if cfg.system_prompt.exists():
        cmd += ["--append-system-prompt", cfg.system_prompt.read_text()]
    cmd += ["-p", _bootstrap_prompt_for_config(cfg, stream, topic)]
    cmd += cfg.backend_flags
    return cmd


def _build_opencode_cmd(cfg: LaunchConfig, stream: str, topic: str) -> list[str]:
    cmd = [
        cfg.agent_command,
        "run",
        "--format", "json",
        "--dir", str(cfg.working_dir.resolve()),
        "--dangerously-skip-permissions",
    ]
    if cfg.opencode_model:
        cmd += ["--model", cfg.opencode_model]
    if cfg.opencode_agent:
        cmd += ["--agent", cfg.opencode_agent]
    cmd += cfg.backend_flags
    cmd.append(_bootstrap_prompt_for_config(cfg, stream, topic))
    return cmd


def _opencode_config_json(cfg: LaunchConfig, env: MutableMapping[str, str]) -> str:
    """Build the OPENCODE_CONFIG_CONTENT JSON string."""
    config: dict[str, Any] = {}
    if cfg.system_prompt.exists():
        config["instructions"] = [str(cfg.system_prompt.resolve())]
    if cfg.mcp_config.exists():
        config["mcp"] = _opencode_mcp_servers(cfg.mcp_config, env)
    return json.dumps(config, separators=(",", ":"))


def _opencode_mcp_servers(
    path: Path, env: MutableMapping[str, str],
) -> dict[str, Any]:
    """Translate Claude .mcp.json servers to OpenCode format."""
    data = json.loads(path.read_text())
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path} must contain an object at mcpServers")

    result: dict[str, Any] = {}
    for name, raw_server in servers.items():
        if not isinstance(raw_server, dict):
            raise ValueError(f"MCP server {name!r} in {path} must be an object")
        server = dict(raw_server)
        oc: dict[str, Any] = {"timeout": OPENCODE_MCP_TIMEOUT_MS}

        if "command" in server:
            oc["type"] = "local"
            command_parts = [_reject_argv_env_refs(str(server["command"]), path, name, "command")]
            command_parts += [
                _reject_argv_env_refs(str(a), path, name, "args")
                for a in _as_list(server.get("args"))
            ]
            oc["command"] = command_parts
        elif "url" in server:
            oc["type"] = "remote"
            oc["url"] = _reject_argv_env_refs(str(server["url"]), path, name, "url")
            headers = _env_map(server.get("headers"))
            if headers:
                oc["headers"] = {k: _expand_env(v, env) for k, v in headers.items()}
        else:
            raise ValueError(f"MCP server {name!r} in {path} must define command or url")

        env_block = _resolve_env_block(server.get("env"), env)
        if env_block:
            oc["environment"] = env_block

        result[name] = oc
    return result


def _resolve_env_block(
    raw: object, env: MutableMapping[str, str],
) -> dict[str, str]:
    """Resolve a Claude env map and propagate values into the process env.

    Side effect: writes resolved values back into ``env`` so later servers
    in the same config can reference them (matches Codex env propagation).
    """
    resolved: dict[str, str] = {}
    for key, value in _env_map(raw).items():
        expanded = _expand_env(str(value), env)
        resolved[key] = expanded
        env[key] = expanded
    return resolved


def _build_codex_cmd(
    cfg: LaunchConfig,
    stream: str,
    topic: str,
    env: MutableMapping[str, str],
) -> list[str]:
    codex_top_level_flags, codex_exec_flags = _split_codex_flags(cfg.backend_flags)
    _append_unique(codex_top_level_flags, "--search")
    cmd = [
        cfg.agent_command,
        *codex_top_level_flags,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--cd",
        str(cfg.working_dir.resolve()),
    ]
    cmd += _codex_permission_args(cfg.codex_permission_mode)
    if cfg.system_prompt.exists():
        cmd += _codex_config_arg("developer_instructions", cfg.system_prompt.read_text())
    if cfg.mcp_config.exists():
        cmd += _codex_mcp_config_args(cfg.mcp_config, env)
    cmd += codex_exec_flags
    cmd.append(_bootstrap_prompt_for_config(cfg, stream, topic))
    return cmd


def _codex_permission_args(mode: str) -> list[str]:
    if mode == "parity":
        return ["--yolo"]
    if mode == "workspace-write":
        return [
            "--sandbox",
            "workspace-write",
            *_codex_config_arg("approval_policy", "never"),
            *_codex_config_arg("sandbox_workspace_write.network_access", True),
        ]
    if mode == "read-only":
        return ["--sandbox", "read-only", *_codex_config_arg("approval_policy", "never")]
    if mode == "none":
        return []
    raise ValueError(f"Unsupported Codex permission mode: {mode!r}")


def _split_codex_flags(flags: list[str]) -> tuple[list[str], list[str]]:
    top_level: list[str] = []
    exec_level: list[str] = []
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag in _CODEX_TOP_LEVEL_ONLY_FLAGS_WITHOUT_VALUE:
            top_level.append(flag)
            i += 1
            continue
        if any(flag.startswith(f"{name}=") for name in _CODEX_TOP_LEVEL_ONLY_FLAGS_WITH_VALUE):
            top_level.append(flag)
            i += 1
            continue
        if flag in _CODEX_TOP_LEVEL_ONLY_FLAGS_WITH_VALUE:
            top_level.append(flag)
            if i + 1 >= len(flags):
                raise ValueError(f"{flag} requires a value")
            top_level.append(flags[i + 1])
            i += 2
            continue
        exec_level.append(flag)
        i += 1
    return top_level, exec_level


def _codex_mcp_config_args(path: Path, env: MutableMapping[str, str]) -> list[str]:
    """Translate Claude-style .mcp.json server entries into Codex -c overrides."""
    data = json.loads(path.read_text())
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path} must contain an object at mcpServers")

    args: list[str] = []
    for name, raw_server in servers.items():
        if not isinstance(raw_server, dict):
            raise ValueError(f"MCP server {name!r} in {path} must be an object")
        server = dict(raw_server)
        server_type = str(server.get("type", "")).lower()
        if server_type == "sse":
            raise ValueError(
                f"MCP server {name!r} uses Claude SSE config. "
                "Codex supports stdio and streamable HTTP MCP servers; use a streamable HTTP URL."
            )

        prefix = _config_key("mcp_servers", str(name))

        if "command" in server:
            args += _codex_config_arg(
                f"{prefix}.command",
                _reject_argv_env_refs(str(server["command"]), path, str(name), "command"),
            )
            if "args" in server:
                args += _codex_config_arg(
                    f"{prefix}.args",
                    [
                        _reject_argv_env_refs(str(v), path, str(name), "args")
                        for v in _as_list(server["args"])
                    ],
                )
            if "cwd" in server:
                args += _codex_config_arg(
                    f"{prefix}.cwd",
                    _reject_argv_env_refs(str(server["cwd"]), path, str(name), "cwd"),
                )
            env_vars = _normalize_env_vars(server.get("env_vars"))
            for key, value in _env_map(server.get("env")).items():
                env[key] = _expand_env(str(value), env)
                _append_unique(env_vars, key)
            _append_inherited_env_vars(env_vars, env)
            if env_vars:
                args += _codex_config_arg(f"{prefix}.env_vars", env_vars)
        elif "url" in server:
            args += _codex_config_arg(
                f"{prefix}.url",
                _reject_argv_env_refs(str(server["url"]), path, str(name), "url"),
            )
            if "bearer_token_env_var" in server:
                args += _codex_config_arg(
                    f"{prefix}.bearer_token_env_var",
                    str(server["bearer_token_env_var"]),
                )
            env_http_headers = _header_env_map(str(name), server.get("headers"), env)
            env_http_headers.update(_header_env_map(str(name), server.get("http_headers"), env))
            env_http_headers.update(_env_map(server.get("env_http_headers")))
            if env_http_headers:
                args += _codex_config_arg(f"{prefix}.env_http_headers", env_http_headers)
        else:
            raise ValueError(f"MCP server {name!r} in {path} must define command or url")

        for key in (
            "enabled",
            "startup_timeout_sec",
            "startup_timeout_ms",
            "enabled_tools",
            "disabled_tools",
            "scopes",
            "oauth_resource",
        ):
            if key in server and server[key] is not None:
                args += _codex_config_arg(f"{prefix}.{key}", server[key])

        if "required" in server:
            args += _codex_config_arg(f"{prefix}.required", server["required"])

        tool_timeout = max(
            _int_or_default(server.get("tool_timeout_sec"), 0),
            CODEX_MCP_TOOL_TIMEOUT_SEC,
        )
        args += _codex_config_arg(f"{prefix}.tool_timeout_sec", tool_timeout)

    return args


def _header_env_map(
    server_name: str,
    headers: object,
    env: MutableMapping[str, str],
) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for header, value in _env_map(headers).items():
        env_name = _header_env_name(server_name, header)
        env[env_name] = _expand_env(str(value), env)
        mapped[header] = env_name
    return mapped


def _header_env_name(server_name: str, header: str) -> str:
    safe_server = re.sub(r"[^A-Za-z0-9]+", "_", server_name).strip("_").upper()
    safe_header = re.sub(r"[^A-Za-z0-9]+", "_", header).strip("_").upper()
    return f"ZULIPMCP_MCP_{safe_server}_{safe_header}"


def _normalize_env_vars(value: object) -> list[object]:
    env_vars: list[object] = []
    for item in _as_list(value):
        if isinstance(item, str):
            _append_unique(env_vars, item)
        elif isinstance(item, dict):
            name = item.get("name")
            if name:
                _append_unique(env_vars, dict(item))
    return env_vars


def _append_inherited_env_vars(env_vars: list[object], env: MutableMapping[str, str]) -> None:
    # Codex stdio servers see only whitelisted vars; mirror full parent env
    # for subprocess parity with Claude's Popen inheritance.
    for key in sorted(env):
        if key:
            _append_unique(env_vars, key)


def _expand_env(value: str, env: MutableMapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise ValueError(f"Environment variable {name!r} is required by MCP config")

    return _ENV_REF_RE.sub(replace, value)


def _reject_argv_env_refs(value: str, path: Path, server_name: str, field: str) -> str:
    match = _ENV_REF_RE.search(value)
    if match is None:
        return value
    raise ValueError(
        f"MCP server {server_name!r} in {path} uses ${{{match.group(1)}}} in {field}. "
        "The .mcp.json translation does not expand environment variables in command, "
        "args, cwd, or url. Move secrets into the env block or header fields."
    )


def _codex_config_arg(key: str, value: object) -> list[str]:
    return ["-c", f"{key}={_toml_value(value)}"]


def _config_key(*parts: str) -> str:
    return ".".join(_toml_key(part) for part in parts)


def _toml_key(key: str) -> str:
    return key if _BARE_TOML_KEY_RE.match(key) else json.dumps(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list) or isinstance(value, tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = [f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()]
        return "{ " + ", ".join(parts) + " }"
    raise TypeError(f"Cannot encode {type(value).__name__} as TOML")


def _as_list(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _env_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected an environment/header mapping")
    return {str(k): str(v) for k, v in value.items()}


def _append_unique(items: list[object], item: object) -> None:
    marker = item.get("name") if isinstance(item, dict) else item
    for existing in items:
        existing_marker = existing.get("name") if isinstance(existing, dict) else existing
        if existing_marker == marker:
            return
    items.append(item)


def _int_or_default(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
