# Hermes Zulip Gateway Plugin

This plugin connects Zulip directly to the Hermes messaging gateway. Each
stream/topic is an independent Hermes session. A user message starts a fresh
turn in that session, so the model never blocks in `listen()` and the
per-turn iteration budget resets normally.

The adapter owns message intake, topic routing, normal response delivery,
typing, and approvals. ZulipMCP remains the explicit tool layer for history,
links, users, reactions, files, and cross-conversation sends.

## Behavior

- A mention activates a topic for the lifetime of the gateway process.
- Follow-ups in an active topic do not need another mention.
- After a gateway restart, one mention reactivates the topic and resumes its
  stored Hermes session.
- The first activation includes up to 20 earlier messages from that topic.
- `:ear:` stays on Hermes's latest response while it waits for the next
  message, then moves to the next response.
- `:stop_sign:` replaces the ear with `:zzz:` to confirm that the topic is
  inactive; mentioning Hermes again clears it.
- Approval reactions are 👍 once, ♾️ for the session, and 👎 reject.
- `:stop_sign:` interrupts and deactivates the topic until the next mention.
- Topics and messages marked `/nobots` or `/nb` are ignored.

## Install from a checkout

The curl installer places Hermes at `~/.hermes/hermes-agent`. Install
ZulipMCP into that environment, then link the platform plugin:

```bash
git clone https://github.com/windborne/zulipmcp.git ~/zulipmcp

~/.local/bin/uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e ~/zulipmcp

mkdir -p ~/.hermes/plugins/platforms
ln -s ~/zulipmcp/hermes_plugin/zulip \
  ~/.hermes/plugins/platforms/zulip
hermes plugins enable zulip-platform
```

Add the bot configuration and an explicit user allowlist to
`~/.hermes/.env`:

```dotenv
ZULIP_RC_PATH=/home/you/.hermes/zulip-bot.zuliprc
ZULIP_ALLOWED_USERS=12345,you@example.com
```

The adapter fails closed unless `ZULIP_ALLOWED_USERS` is set or
`ZULIP_ALLOW_ALL_USERS=true` is explicitly enabled.
`ZULIP_ALLOWED_STREAMS` can further restrict the bot to named streams.

## ZulipMCP tools

Add ZulipMCP to `~/.hermes/config.yaml`, excluding lifecycle tools already
owned by the gateway:

```yaml
mcp_servers:
  zulip:
    command: /home/you/.hermes/hermes-agent/venv/bin/python
    args: ["-m", "zulipmcp.mcp"]
    env:
      ZULIP_RC_PATH: ${ZULIP_RC_PATH}
      BOT_ALLOWED_PRIVATE_STREAMS: "__ALL__"
      BOT_ALLOWED_WRITE_STREAMS: "__ALL__"
    tools:
      exclude:
        - set_context
        - reply
        - listen
        - end_session
        - typing
        - stop_typing
      resources: false
      prompts: false

platform_toolsets:
  zulip:
    - hermes-cli
    - mcp-zulip
```

Replace `__ALL__` with comma-separated stream names to narrow the MCP tool
policy.

## Verify

```bash
hermes plugins list
hermes mcp test zulip
hermes gateway status
hermes gateway run
```

For a background service, run `hermes gateway install` once and
`hermes gateway restart` after configuration changes.

## Settings

| Variable | Default | Meaning |
|---|---:|---|
| `ZULIP_REQUIRE_MENTION` | `true` | Require a mention to activate a topic |
| `ZULIP_INITIAL_HISTORY_MESSAGES` | `20` | Same-topic messages added on activation |
| `ZULIP_LISTEN_REACTION` | `ear` | Emoji on the latest response while waiting; empty disables it |
| `ZULIP_STOPPED_REACTION` | `zzz` | Emoji confirming that a topic was deactivated; empty disables it |
| `ZULIP_DISMISS_REACTION` | `stop_sign` | Interrupt and deactivate a topic |
| `ZULIP_MAX_MESSAGE_LENGTH` | `10000` | Outbound chunk size |
