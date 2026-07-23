# zulipmcp

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](https://github.com/windborne/zulipmcp/blob/main/LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue)](https://www.python.org/downloads/)

Run AI agents in Zulip as @mentionable bots — or wire into any [MCP](https://modelcontextprotocol.io/) client. Also works as a Python library.

<img width="1446" height="752" alt="zulipmcp in action" src="https://github.com/user-attachments/assets/6e6bbed7-ed19-4c4a-a9f2-48468dc9a570" />

## Quickstart

1. Install the package:

   ```bash
   uv add zulipmcp --git https://github.com/windborne/zulipmcp.git
   ```

2. Add a `.zuliprc` file to your project root with your Zulip bot credentials. See [Add a bot or integration](https://zulip.com/help/add-a-bot-or-integration) for instructions on making a bot. The bot type must be "generic."

3. Add the MCP server to your `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "zulip": {
         "command": "uv",
         "args": ["run", "python", "-m", "zulipmcp.mcp"]
       }
     }
   }
   ```

4. Restart your MCP client. The Zulip tools should now be available.

## Requirements

- Python >=3.10, managed with [uv](https://docs.astral.sh/uv/)
- A `.zuliprc` file for Zulip API auth (see [Quickstart](#quickstart))
- For listener mode: the selected backend CLI installed and authenticated (`claude` by default, `codex` with `--backend codex`, or `opencode` with `--backend opencode`)

## Entry Points

| Entry Point | Description |
|---|---|
| `uv run python -m zulipmcp.mcp` | MCP server for Claude Code, Codex, and other MCP clients |
| `uv run python -m zulipmcp.mcp --transport sse` | MCP server over SSE (for remote/web clients) |
| `uv run python -m zulipmcp.listener` | Listener: watches for @mentions, spawns agent sessions |

## Library Usage

zulipmcp can also be imported directly as a Python library:

```python
import zulipmcp

# Fetch and format recent messages
messages = zulipmcp.get_messages(hours_back=24, channels=["engineering"])
print(zulipmcp.format_messages(messages))

# Send a message
zulipmcp.send_message("engineering", "general", "Hello from Python!")

# Configure MCP hooks before starting the server
zulipmcp.configure(
    message_prefix=lambda: "[bot] ",
    on_session_end=lambda session: print(f"Session ended in #{session.stream}"),
)
```

## Listener

The optional `zulipmcp.listener` module watches Zulip for @mentions and spawns one headless agent session per (stream, topic). It supports Claude Code by default, Codex with `--backend codex`, and [OpenCode](https://opencode.ai/) with `--backend opencode`. It's the glue between Zulip events and the agent backend -- the MCP server handles all the Zulip tools, the listener just handles lifecycle.

```bash
# Minimal -- uses ./.zuliprc, ./.mcp.json (if present), and the bundled default prompt
uv run python -m zulipmcp.listener

# Full -- override MCP config and system prompt
uv run python -m zulipmcp.listener \
    --mcp-config .mcp.json \
    --system-prompt agent.md \
    --log-dir ./logs

# Recommended: Claude Code with Opus 4.6
uv run python -m zulipmcp.listener -- --model claude-opus-4-6

# Recommended: Codex with GPT-5.5 and medium reasoning
uv run python -m zulipmcp.listener --backend codex -- \
    --model gpt-5.5 \
    -c 'model_reasoning_effort="medium"'

# Recommended: OpenCode with any provider (Qwen, Llama, Gemini, etc.)
uv run python -m zulipmcp.listener --backend opencode \
    --opencode-model anthropic/claude-sonnet-4-5

uv run python -m zulipmcp.listener --backend opencode \
    --opencode-model ollama/qwen3:235b

# Pass additional backend-specific flags after --
uv run python -m zulipmcp.listener -- --strict-mcp-config --effort medium
uv run python -m zulipmcp.listener --backend codex -- -c 'model_verbosity="low"'
uv run python -m zulipmcp.listener --backend opencode -- --verbose
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--zuliprc` | `./.zuliprc` | Path to `.zuliprc` (resolved relative to current working directory) |
| `--backend` | `claude` | Agent backend to launch: `claude`, `codex`, or `opencode` |
| `--agent-command` | backend name | Backend CLI binary name or path |
| `--mcp-config` | `./.mcp.json` | Path to `.mcp.json` for agent sessions (used only if the file exists). Codex translates supported `command` and `url` servers into one-run `-c mcp_servers...` overrides. OpenCode embeds the translated config in `OPENCODE_CONFIG_CONTENT`. |
| `--system-prompt` | `zulipmcp/default_system_prompt.md` | System prompt file. Claude receives it as an appended system prompt; Codex receives it as developer instructions; OpenCode receives it via the `instructions` config field. |
| `--working-dir` | `.` | Working directory for spawned sessions |
| `--log-dir` | `./logs` | Directory for session log files |
| `--codex-permission-mode` | `parity` | Codex-only permission preset. `parity` uses `--yolo` for full bypass like the Claude default and assumes external sandboxing; `workspace-write` and `read-only` use noninteractive sandboxed modes; `none` adds no permission flags. |
| `--opencode-model` | *(none)* | OpenCode model in `provider/model` format (e.g. `anthropic/claude-sonnet-4-5`, `ollama/qwen3:235b`). When omitted, OpenCode uses its own default. |
| `--opencode-agent` | *(none)* | OpenCode agent name (passed as `--agent`). When omitted, OpenCode uses its default agent. |
| `-- ...` | *(none)* | Everything after `--` is forwarded to the selected backend as-is. For Codex, known top-level-only flags are placed before `exec` automatically. |

Each session gets `TRIGGER_MESSAGE_ID` and `SESSION_USER_EMAIL` set automatically so `set_context()` anchors to the @mention and hooks can identify the requester.

The listener intentionally does not set model or reasoning defaults. Backend CLIs and model aliases move over time, so use the backend's user config or pass flags after `--`. For reproducible production behavior, pin exact backend model IDs in your deployment config instead of relying on aliases.

Custom `--system-prompt` files are backend instructions, not the initial task. The listener still sends a short per-session bootstrap prompt with the target stream/topic and the Zulip lifecycle contract: initialize context, send visible text through `reply()`, then call `listen()` when yielding for follow-ups.

Codex sessions launch with web search enabled to match Claude Code's default web-fetch capability. For Codex, the `.mcp.json` adapter whitelists inherited environment variable names for translated stdio MCP servers, mirroring Claude-style subprocess inheritance without putting env values in argv. It also forwards Zulip's direct auto-init `SESSION_STREAM`/`SESSION_TOPIC` pair when present and sets `tool_timeout_sec` to at least 3 hours so the long-running `listen()` tool can wait for follow-ups. The adapter is intentionally conservative: Claude SSE config is rejected, only `command` and streamable HTTP `url` servers are translated, and environment placeholders are supported only in env/header values that can stay out of process argv.

OpenCode sessions receive the full config via `OPENCODE_CONFIG_CONTENT` (inline JSON). The `.mcp.json` adapter translates `command`-based servers to OpenCode's `local` type (merging `command`+`args` into a single array) and `url`-based servers to `remote` type, renaming `env` to `environment`. Each translated server gets a 3-hour MCP timeout so `listen()` can block for follow-ups; `listen()` sends MCP progress notifications during its long-poll loop, which OpenCode uses to reset its per-call timeout. The system prompt file path is passed via the `instructions` config field.

The listener is deliberately minimal. It omits concurrency caps, workspace isolation, staleness watchdogs, and dashboards -- add those when you need them.

## Key Design Details

### Listening for messages

The `listen` tool uses Zulip's [real-time events API](https://zulip.com/api/real-time-events) (long-polling) instead of repeated `GET /messages` calls. On entry it catches up on any messages since `last_seen_message_id`, subscribes the bot to the stream if needed, registers a narrowed event queue for the stream/topic, and then long-polls via `GET /events`. The server blocks until a message arrives or ~90 seconds elapse (heartbeat), making this ~30x more efficient than polling every 2 seconds. If the queue expires (`BAD_EVENT_QUEUE_ID`), it re-registers automatically. The queue is deleted in a `finally` block on exit.

An `ear` emoji is added to the last message as a visual indicator while listening and removed when listening stops. If the realm has a custom emoji named `robot_ear`, that is used instead. MCP keepalive pings are sent via `ctx.info()` after each long-poll cycle.

### No missed messages on reply

When `reply` is called, it checks for new messages *before* sending. If anyone posted while the LLM was thinking, those messages are fetched and returned alongside the "message sent" confirmation. This way the LLM always sees what it missed and can react accordingly. The `last_seen_message_id` is updated to whichever is newest -- the missed messages or the sent message -- so nothing falls through the cracks.

### Session dismissal

Users can dismiss a bot session by reacting with a configurable emoji (default: `:stop_sign:`) on any bot message. The dismiss check runs both during `listen()` (via reaction events) and before `reply()` (via REST API poll), covering the race condition where a user reacts while the bot is busy working. Customize with `configure(dismiss_emoji={"stop_sign", "wave"})`.

### Bot visibility filtering

Topics containing `/nobots` or `/nb` are hidden from the bot entirely. Messages starting with `/nobots` or `/nb` are also filtered out. This lets humans have private conversations the bot won't see.

## Environment Variables

| Variable | Description |
|---|---|
| `ZULIP_RC_PATH` | Absolute path to `.zuliprc` for direct MCP server use. Listener mode sets this for spawned sessions from `--zuliprc`; it does not read ambient `ZULIP_RC_PATH` as its own default. |
| `ZULIPMCP_TIMEZONE` | IANA timezone used to display message timestamps, such as `Europe/London` or `Asia/Tokyo`. Defaults to `America/Los_Angeles`. |
| `ZULIP_MAX_MESSAGE_LENGTH` | Char limit above which send tools return an error instead of letting Zulip silently truncate. Defaults to `10000` (Zulip's default); set for realms with a custom cap. |
| `TRIGGER_MESSAGE_ID` | Message ID that triggered the session (e.g. the @mention). Sets the listen anchor so the agent doesn't miss messages after the trigger. |
| `SESSION_USER_EMAIL` | Email of the human who triggered the session. Stored on `SessionState` for hooks. |
| `SESSION_STREAM` | Stream name for auto-initializing a session on server start (direct `run_server()` callers only -- the listener does not use these). Both `SESSION_STREAM` and `SESSION_TOPIC` must be set; the agent can then skip `set_context()`. |
| `SESSION_TOPIC` | Topic for auto-init. Requires `SESSION_STREAM`. |
| `BOT_ALLOWED_PRIVATE_STREAMS` | Private-stream read/send allowlist. Unset = no private-stream access. Accepts `__ALL__`, a JSON list, or comma-separated names. |
| `BOT_ALLOWED_WRITE_STREAMS` | Stream send allowlist. Unset = writes allowed everywhere (backwards-compatible). Same formats as above. |
| `ZULIPMCP_CACHE_DIR` | Override the disk cache directory (defaults to system temp dir). |
| `ZULIPMCP_LOG_DIR` | Override the log directory (defaults to `/tmp/zulipmcp_logs`). |

## License

[Apache 2.0](LICENSE)
