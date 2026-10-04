# Backlog

This file records deferred work and intentional capability gaps. The major
administration features in the original task brief are otherwise complete.
`get_administration_capabilities` remains the authoritative runtime view
because support also depends on the Zulip feature level and principal.

## Planned work

### FastMCP 4

Migrate from the currently supported FastMCP 3.x line in a focused change.
The known source changes are small: move `ToolResult` imports to
`fastmcp.tools` and use snake-case annotation attributes in tests. The
migration still needs installed-wheel and client acceptance tests because
FastMCP 4 replaces the underlying MCP SDK, adopts `httpx2`, and negotiates
the new sessionless protocol.

Verify stdio and SSE startup, Codex and Claude approval behavior, all tool
schemas and structured results, the Hermes integration, and compatibility
with older MCP clients before removing the `<4` dependency constraint.

### Secure reusable invitation creation

Reusable signup URLs are bearer credentials. Add creation only after there
is a delivery mechanism that sends the URL directly to an authorized
operator or secret store without placing it in model-visible MCP results,
logs, traces, or snapshots. Keep audit redaction and revocation coverage.

### Diagnose live email invitation failures

The test realm accepted invitation dry runs but returned HTTP 400 with
“We weren't able to invite anyone” for an allowed, non-member address. A
pending invitation was not created. Reproduce with server-log access and a
controlled recipient, determine whether the cause is mail delivery, realm
policy, or request compatibility, and add a regression test only if the
client is at fault.

### Opt-in live acceptance harness

Turn the manual disposable-realm checks into an explicit opt-in harness.
Cover reversible channel, membership, group, bot, emoji, branding,
moderation, and export flows, with restoration and retained-artifact
reporting. Never make normal CI depend on live credentials or external
email delivery.

### Refactoring

Split the administration implementation out of the large `core.py` and
`mcp.py` modules while preserving the MCP/core boundary and public schemas.
Consider generating the documentation inventory from registered tools and
capability metadata so names, gate assignments, and feature floors cannot
drift. A declarative organization-planning helper may compose existing
dry-run operations, but it must not become a blind cross-realm clone.

### Distribution checks

Add CI coverage that builds and installs the wheel in an isolated
environment, checks bundled package data, starts both module entry points,
and enumerates the MCP tools. This protects against dependency-major drift
and missing non-Python resources.

### Hermes packaging and compatibility

Decide whether the Hermes gateway plugin should remain checkout-only or be
included in a distribution artifact. Validate the documented configuration
and CLI commands against a named supported Hermes release, and add an
acceptance check for stream/topic and direct-message routing.

## Intentional and upstream limits

Retain these as explicit capability results unless Zulip adds a supported
API or a safe credential design becomes available:

- Organization deactivation is intentionally not an ordinary model-facing
  action. Reactivation requires a server management command, and Zulip has
  no standalone organization-deletion REST endpoint.
- Zulip exposes message reporting but no dedicated report queue or
  report-resolution API.
- Zulip documents no API for resetting branding to server defaults or
  reactivating custom emoji. Saved branding can instead be downloaded and
  uploaded again.
- Bot API-key retrieval and regeneration remain outside normal MCP output.
- Export download URLs remain redacted bearer credentials, and this server
  does not download export archives.
