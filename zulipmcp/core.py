import os
import re
import time
import tempfile
import json
import hashlib
import logging
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import BinaryIO, Callable, Iterator, Optional

import diskcache
import requests
import zulip

from .configuration import (
    APIError,
    CHANNEL_CREATE_FIELDS,
    CHANNEL_CREATE_RESIDUAL_FIELDS,
    CHANNEL_GROUP_FIELDS,
    CHANNEL_REFERENCE_REALM_FIELDS,
    CHANNEL_UPDATE_FIELDS,
    CONFIGURATION_FETCH_EVENT_TYPES,
    ConfigurationQueueSnapshot,
    CURRENT_USER_FIELDS,
    JSONValue,
    DEFAULT_USER_WRITE_FIELDS,
    GROUP_SETTING_REALM_FIELDS,
    LINKIFIER_REVERSE_FIELDS,
    MutationResult,
    MutationStatus,
    ORGANIZATION_SECTIONS,
    OWNER_ONLY_REALM_FIELDS,
    PROFILE_FIELD_CREATE_FIELDS,
    PROFILE_FIELD_UPDATE_FIELDS,
    QUEUE_SECTIONS,
    REALM_WRITE_FIELDS,
    UNLIMITED_REALM_FIELDS,
    USER_GROUP_PERMISSION_FIELDS,
    USER_GROUP_UPDATE_FIELDS,
    SectionResult,
    SectionStatus,
    ZulipAPIError,
    annotate_channel_configuration,
    partition_realm_snapshot,
    project_bot_inventory,
    project_user_inventory,
    read_section,
    redact_secrets,
    resolve_permission_groups,
    sanitize_text,
)

_DEFAULT_TIMEZONE = "America/Los_Angeles"
_logger = logging.getLogger(__name__)
_response_status: ContextVar[int | None] = ContextVar("response_status", default=None)


def _capture_response_status(response: requests.Response, *args: object, **kwargs: object) -> None:
    _response_status.set(response.status_code)


def _enable_response_status_tracking(client: zulip.Client) -> None:
    client.ensure_session()
    assert client.session is not None
    hooks = client.session.hooks.setdefault("response", [])
    if _capture_response_status not in hooks:
        hooks.append(_capture_response_status)


def _load_timezone() -> ZoneInfo:
    """Load the configured IANA timezone."""
    name = os.environ.get("ZULIPMCP_TIMEZONE", _DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"Invalid ZULIPMCP_TIMEZONE {name!r}; use an IANA timezone name"
        ) from exc


TIMEZONE = _load_timezone()

_client: Optional[zulip.Client] = None
_cache = diskcache.Cache(os.environ.get("ZULIPMCP_CACHE_DIR",
    Path(tempfile.gettempdir()) / "zulipmcp_cache"))
_ignored_streams: set[str] = set()
_ALL_PRIVATE_STREAMS = "__ALL__"
_dismiss_emoji: set[str] = {"stop_sign"}

# Zulip silently truncates messages longer than this server-side, so a sender
# never learns its message was clipped. Matches Zulip's default; override via
# env for realms configured with a different cap. Bad values fall back rather
# than crashing the server at import.
try:
    MAX_MESSAGE_LENGTH = int(os.environ.get("ZULIP_MAX_MESSAGE_LENGTH", "10000"))
except ValueError:
    MAX_MESSAGE_LENGTH = 10000

_FENCE_RE = re.compile(r'^(`{3,}|~{3,})')
# Inline code spans with variable-length delimiters (`` `x` ``, ``` ``x`` ```),
# mirroring Python-Markdown's backtick pairing.
_INLINE_CODE_RE = re.compile(r'(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)')
# Link URLs allow one level of balanced parens (GFM-style) so targets like
# .../wiki/Foo_(bar) terminate at the real closing paren.
_LINK_RE = re.compile(r'(?<!!)\[([^\]]*)\]\((<?(?:\([^()\s]*\)|[^()\s])+>?)\)')
_URL_ABUTS_BOLD_RE = re.compile(r'(https?://[^\s<>\[\]()*]+)(?=\*\*)')


def _autofix_enabled() -> bool:
    """Whether outgoing markdown normalization is enabled.

    Controlled by ``ZULIPMCP_MARKDOWN_AUTOFIX``: set to ``0`` or ``false``
    (case-insensitive) to disable all normalization.  Read at call time so
    it can be toggled without a restart.
    """
    return os.environ.get('ZULIPMCP_MARKDOWN_AUTOFIX', '').lower() not in ('0', 'false')


def _is_table_separator(line: str) -> bool:
    """Whether *line* is a markdown table separator (e.g. ``| --- | --- |``)."""
    stripped = line.strip()
    if not stripped or '-' not in stripped:
        return False
    inner = stripped.lstrip('|').rstrip('|')
    return bool(inner) and set(inner) <= set('|:- ')


def _restyle_bold_in_link(m: 're.Match[str]') -> str:
    """Rewrite bold inside a markdown link's text so Zulip renders it.

    Whole-text bold moves outside the link (``[**a**](url)`` becomes
    ``**[a](url)**``); partial bold is stripped from the text.
    """
    text, url = m.group(1), m.group(2)
    if '**' not in text:
        return m.group(0)
    if text.startswith('**') and text.endswith('**') and text.count('**') == 2:
        return f'**[{text[2:-2]}]({url})**'
    return f'[{text.replace("**", "")}]({url})'


def _sub_outside_code(pattern: 're.Pattern[str]',
                      repl: Callable[['re.Match[str]'], str], line: str) -> str:
    """Apply ``pattern.sub(repl, line)``, leaving matches inside inline code alone.

    A match is exempt only when wholly enclosed in an inline code span; a
    match that merely *contains* a code span is still rewritten.
    """
    code_spans = [span.span() for span in _INLINE_CODE_RE.finditer(line)]

    def guarded(m: 're.Match[str]') -> str:
        if any(s <= m.start() and m.end() <= e for s, e in code_spans):
            return m.group(0)
        return repl(m)

    return pattern.sub(guarded, line)


def _fix_bold_links(line: str) -> str:
    """Rewrite bold/link combinations that Zulip's renderer breaks on."""
    if '**' not in line:
        return line
    line = _sub_outside_code(_LINK_RE, _restyle_bold_in_link, line)
    line = _sub_outside_code(_URL_ABUTS_BOLD_RE,
                             lambda m: f'[{m.group(1)}]({m.group(1)})', line)
    return line


def normalize_zulip_markdown(content: str) -> str:
    """Normalize outgoing markdown for Zulip's renderer.

    Two deterministic fixes for patterns LLMs trained on GFM emit constantly:

    1. Blank lines before tables.  Zulip's Python-Markdown parser requires a
       blank line before table header rows; injects the missing one.
    2. Bold/link combos Zulip breaks on.  Bold inside link text
       (``[**a**](url)``) renders as literal escaped asterisks with an
       unclickable link (zulip/zulip#36087, fix PR stalled upstream) —
       whole-text bold moves outside the link, partial bold is stripped.
       A bare URL immediately followed by ``**`` is wrapped as
       ``[url](url)``: a trailing ``**`` otherwise gets swallowed into the
       autolinked URL, and a URL *preceded* by ``**`` never autolinks at
       all, so the rewrite also upgrades bold URLs to clickable links.

    Both fixes skip fenced code blocks and indented code.  The bold/link
    fix also skips inline code spans; the table fix also skips blockquotes.
    Accepted edge case: a URL whose path literally contains ``**`` is
    truncated at the asterisks (literal ``*`` in URLs should be
    percent-encoded).

    Set ``ZULIPMCP_MARKDOWN_AUTOFIX=0`` (or ``false``) to disable all
    normalization and send content verbatim.
    """
    if not _autofix_enabled():
        return content

    lines = content.split('\n')
    result: list[str] = []
    in_fence = False
    fence_char: str | None = None
    fence_len = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        m = _FENCE_RE.match(stripped)
        if m:
            fence_chars = m.group(1)
            after_fence = stripped[len(fence_chars):]
            if not in_fence:
                in_fence, fence_char, fence_len = True, fence_chars[0], len(fence_chars)
            elif fence_chars[0] == fence_char and len(fence_chars) >= fence_len and not after_fence:
                in_fence = False
                fence_char = None
            result.append(line)
            continue

        if in_fence:
            result.append(line)
            continue

        if (
            stripped
            and '|' in stripped
            and result
            and result[-1].strip()
            and not stripped.startswith('>')
            and not line.startswith('    ')
            and not line.startswith('\t')
            and i + 1 < len(lines)
            and _is_table_separator(lines[i + 1])
        ):
            result.append('')

        if not line.startswith('    ') and not line.startswith('\t'):
            line = _fix_bold_links(line)

        result.append(line)

    return '\n'.join(result)


def _is_allow_all_token(value: object) -> bool:
    """Whether a value is the special allow-all sentinel."""
    return isinstance(value, str) and value.strip().upper() == _ALL_PRIVATE_STREAMS


def _parse_stream_allowlist(env_var: str, default_all: bool) -> set[str]:
    """Parse a stream allowlist env var into a lowercase set."""
    raw = os.environ.get(env_var, "").strip()
    if not raw:
        return {_ALL_PRIVATE_STREAMS} if default_all else set()
    if _is_allow_all_token(raw):
        return {_ALL_PRIVATE_STREAMS}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            if any(_is_allow_all_token(s) for s in parsed):
                return {_ALL_PRIVATE_STREAMS}
            return {str(s).lower() for s in parsed if str(s).strip()}
        if isinstance(parsed, str):
            if _is_allow_all_token(parsed):
                return {_ALL_PRIVATE_STREAMS}
            return {parsed.lower()}
    except json.JSONDecodeError:
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    return set()


def _allowed_private_streams() -> set[str]:
    """Return allowed private streams from env.

    Security model:
    - Unset/empty env => no private stream access (default-deny)
    - "__ALL__" => explicit full private stream access
    - list/string => explicit allowlist (normalized to lowercase)
    """
    return _parse_stream_allowlist("BOT_ALLOWED_PRIVATE_STREAMS", default_all=False)


def _allowed_write_streams() -> set[str]:
    """Return allowed write streams from env.

    Security model:
    - Unset/empty env => all stream writes allowed (backwards-compatible)
    - "__ALL__" => explicit full write access
    - list/string => explicit allowlist (normalized to lowercase)
    """
    return _parse_stream_allowlist("BOT_ALLOWED_WRITE_STREAMS", default_all=True)


def is_private_stream_allowed(stream_name: str) -> bool:
    """Whether the current process is allowed to access this private stream."""
    if not is_stream_private(stream_name):
        return True
    allowed = _allowed_private_streams()
    if _ALL_PRIVATE_STREAMS in allowed:
        return True
    return stream_name.lower() in allowed


def is_stream_write_allowed(stream_name: str) -> bool:
    """Whether the current process is allowed to send to this stream."""
    allowed = _allowed_write_streams()
    if _ALL_PRIVATE_STREAMS in allowed:
        return True
    return stream_name.lower() in allowed


def _stream_write_error(stream: str) -> Optional[dict]:
    """Return an API-style error dict if writes to this stream are forbidden."""
    if is_stream_private(stream) and not is_private_stream_allowed(stream):
        return {"result": "error", "msg": f"Private stream access denied: {stream}"}
    if not is_stream_write_allowed(stream):
        return {"result": "error", "msg": f"Stream write access denied: {stream}"}
    return None


def _get_stream_message_for_write(message_id: int) -> tuple[Optional[dict], Optional[dict]]:
    """Return a stream message or an API-style error dict for write operations."""
    msg = get_message_by_id(message_id)
    if not msg:
        return None, {"result": "error", "msg": f"Message {message_id} not found or inaccessible"}
    if msg.get("type") != "stream":
        return None, {"result": "error", "msg": "This operation only supports stream messages"}
    stream = msg.get("display_recipient", "")
    if not stream:
        return None, {"result": "error", "msg": f"Message {message_id} stream not found"}
    err = _stream_write_error(stream)
    if err:
        return None, err
    return msg, None


def set_ignored_streams(streams: set[str]) -> None:
    """Set streams to exclude from all message fetches."""
    global _ignored_streams
    _ignored_streams = {s.lower() for s in streams}


def get_ignored_streams() -> set[str]:
    """Return the current ignored streams set."""
    return _ignored_streams


def set_dismiss_emoji(emoji: set[str] | str) -> None:
    """Set the emoji names that trigger session dismissal when reacted.

    Pass an empty set to disable dismiss-by-reaction.
    """
    global _dismiss_emoji
    if isinstance(emoji, str):
        emoji = {emoji}
    _dismiss_emoji = {e.strip(":") for e in emoji if e.strip(":")}


def get_dismiss_emoji() -> set[str]:
    """Return the current dismiss emoji set."""
    return _dismiss_emoji


def is_dismiss_reaction(event: dict, bot_user_id: int,
                        stream: Optional[str] = None,
                        topic: Optional[str] = None) -> bool:
    """Check if a reaction event is a dismiss signal on a bot-authored message.

    Returns True if:
    - The reaction is an "add" operation
    - The emoji matches a dismiss emoji
    - The reactor is not the bot itself
    - The reacted-on message was sent by the bot
    - The reacted-on message is in the given stream/topic (if specified)

    The last checks require one API call to fetch the message.

    Note: Zulip's event queue narrow does NOT filter reaction events, so
    the caller receives reactions from all streams.  Pass stream/topic to
    ensure only reactions in the current conversation trigger dismissal.
    """
    if event.get("op") != "add":
        return False
    if event.get("emoji_name") not in _dismiss_emoji:
        return False
    if event.get("user_id") == bot_user_id:
        return False

    # Verify the reacted-on message was sent by this bot (and optionally
    # that it belongs to the expected stream/topic).
    msg_id = event.get("message_id")
    if not msg_id:
        return False
    try:
        result = get_client().call_endpoint(
            url=f"/messages/{msg_id}",
            method="GET",
        )
        if result.get("result") != "success":
            return False
        msg = result.get("message", {})
        if msg.get("sender_id") != bot_user_id:
            return False
        # Verify stream/topic if provided.
        # display_recipient is a string for stream messages but a list for DMs.
        display_recipient = msg.get("display_recipient", "")
        if stream:
            if not isinstance(display_recipient, str) or display_recipient.lower() != stream.lower():
                return False
        if topic and msg.get("subject", "").lower() != topic.lower():
            return False
        return True
    except Exception:
        return False


def check_dismissed(message_id: int, bot_user_id: int) -> Optional[str]:
    """Check if a bot message already has a dismiss reaction.

    Fetches the message via REST API and inspects its reactions list.
    Returns the emoji name if a dismiss reaction from a non-bot user exists,
    or None if no dismiss reaction is found.

    This catches the race condition where a user reacts while the bot is
    busy (not in listen()), so the reaction event was never consumed.
    """
    if not _dismiss_emoji:
        return None
    try:
        result = get_client().call_endpoint(
            url=f"/messages/{message_id}",
            method="GET",
        )
        if result.get("result") != "success":
            return None
        msg = result.get("message", {})
        for reaction in msg.get("reactions", []):
            if reaction.get("emoji_name") in _dismiss_emoji and reaction.get("user_id") != bot_user_id:
                return reaction["emoji_name"]
        return None
    except Exception:
        return None


def is_stream_private(stream_name: str) -> bool:
    """Check if a stream is private (invite_only). Cached for 1 hour."""
    cache_key = ("is_stream_private", stream_name.lower())
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    # Fetch all streams and cache every one — avoids repeated API calls
    result = get_client().get_streams(include_public=True, include_subscribed=True)
    if result["result"] != "success":
        return True  # can't determine — assume private (safer)

    for s in result.get("streams", []):
        is_private = s.get("invite_only", False)
        _cache.set(("is_stream_private", s["name"].lower()), is_private, expire=3600)

    # Re-check cache after populating
    cached = _cache.get(cache_key)
    return cached if cached is not None else True  # not in results — must be private


def get_client() -> zulip.Client:
    """Get or create the Zulip client singleton.

    Config resolution order:
        1. ZULIP_RC_PATH environment variable (absolute path to zuliprc)
        2. .zuliprc in the current working directory
    """
    global _client
    if _client is None:
        import os
        import sys
        rc_env = os.environ.get("ZULIP_RC_PATH")
        if rc_env:
            config_path = Path(rc_env)
        else:
            config_path = Path.cwd() / ".zuliprc"
        if not config_path.exists():
            raise FileNotFoundError(
                f"zuliprc not found at: {config_path}\n"
                f"Set ZULIP_RC_PATH env var or place .zuliprc in the working directory."
            )
        _client = zulip.Client(config_file=str(config_path))
        # Log identity so it's visible in MCP startup output
        email = _client.email or "unknown"
        print(f"[zulipmcp] Zulip client initialized as: {email} (from {config_path})", file=sys.stderr)
    return _client


def configuration_request(
    url: str,
    method: str = "GET",
    request: dict[str, JSONValue] | None = None,
    files: list[BinaryIO] | None = None,
) -> dict[str, JSONValue]:
    client = None
    status_token = _response_status.set(None)
    try:
        client = get_client()
        if isinstance(client, zulip.Client):
            _enable_response_status_tracking(client)
        kwargs: dict[str, object] = {"url": url, "method": method, "request": request}
        if files is not None:
            kwargs["files"] = files
        response = client.call_endpoint(**kwargs)
        response_status = _response_status.get()
    except (requests.RequestException, OSError, zulip.UnrecoverableNetworkError) as exc:
        key = getattr(client, "api_key", None)
        secrets = [key] if isinstance(key, str) else []
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if not isinstance(status, int):
            status = _response_status.get()
        raise ZulipAPIError(APIError(
            message=sanitize_text(str(exc), secrets),
            code="TRANSPORT_ERROR",
            http_status=status if isinstance(status, int) else None,
        )) from None
    finally:
        _response_status.reset(status_token)
    if not isinstance(response, dict):
        raise ZulipAPIError(APIError(
            message="Zulip API returned a non-object response",
            code="INVALID_RESPONSE",
        ))
    if response.get("result") != "success":
        error = APIError.from_response(response)
        key = getattr(client, "api_key", None)
        secrets = [key] if isinstance(key, str) else []
        raise ZulipAPIError(APIError(
            message=sanitize_text(error.message, secrets),
            code=error.code,
            http_status=error.http_status or response_status,
        ))
    data = redact_secrets({
        key: value for key, value in response.items() if key not in {"result", "msg"}
    })
    assert isinstance(data, dict)
    return data


def get_server_settings() -> dict[str, JSONValue]:
    return configuration_request("/server_settings")


def get_current_user() -> dict[str, JSONValue]:
    return configuration_request("/users/me")


def get_streams_configuration(
    include_all: bool = True,
    include_default: bool = True,
    include_web_public: bool | None = None,
    exclude_archived: bool = False,
) -> dict[str, JSONValue]:
    request: dict[str, JSONValue] = {
        "include_all": include_all,
        "include_default": include_default,
        "exclude_archived": exclude_archived,
    }
    if include_web_public is not None:
        request["include_web_public"] = include_web_public
    return configuration_request("/streams", request=request)


def get_users_configuration() -> dict[str, JSONValue]:
    return configuration_request(
        "/users",
        request={"include_custom_profile_fields": True},
    )


def get_user_groups_configuration() -> dict[str, JSONValue]:
    return configuration_request(
        "/user_groups",
        request={"include_deactivated_groups": True},
    )


def get_profile_fields_configuration() -> dict[str, JSONValue]:
    return configuration_request("/realm/profile_fields")


def get_domains_configuration() -> dict[str, JSONValue]:
    return configuration_request("/realm/domains")


def get_linkifiers_configuration() -> dict[str, JSONValue]:
    return configuration_request("/realm/linkifiers")


def get_emoji_configuration() -> dict[str, JSONValue]:
    return configuration_request("/realm/emoji")


def get_invitations_configuration() -> dict[str, JSONValue]:
    return configuration_request("/invites")


def get_channel_subscribers_configuration(stream_id: int) -> dict[str, JSONValue]:
    return configuration_request(f"/streams/{stream_id}/members")


def get_streams_audit(
    include_all: bool = True,
    include_default: bool = True,
    include_web_public: bool | None = None,
    exclude_archived: bool = False,
) -> SectionResult:
    section = read_section(
        lambda: get_streams_configuration(
            include_all=include_all,
            include_default=include_default,
            include_web_public=include_web_public,
            exclude_archived=exclude_archived,
        ),
        collection_field="streams",
    )
    if section.status not in {SectionStatus.OK, SectionStatus.PARTIAL}:
        return section
    if not isinstance(section.data, dict):
        return section
    streams = section.data.get("streams")
    if not isinstance(streams, list) or not streams:
        return section

    group_names: dict[int, str] = {}
    try:
        group_data = get_user_groups_configuration()
    except ZulipAPIError as exc:
        section.status = SectionStatus.PARTIAL
        section.warnings.append(exc.error.message)
    else:
        groups = group_data.get("user_groups")
        if isinstance(groups, list):
            group_names = {
                group["id"]: group["name"]
                for group in groups
                if isinstance(group, dict)
                and isinstance(group.get("id"), int)
                and isinstance(group.get("name"), str)
            }
        else:
            section.status = SectionStatus.PARTIAL
            section.warnings.append("User groups were absent or null")
    section.data = annotate_channel_configuration(section.data, group_names)

    try:
        server_settings = get_server_settings()
    except ZulipAPIError as exc:
        section.status = SectionStatus.PARTIAL
        section.warnings.append(
            f"Could not determine channel field capabilities: {exc.error.message}"
        )
    else:
        feature_level = server_settings.get("zulip_feature_level")
        if isinstance(feature_level, int) and not isinstance(feature_level, bool):
            if feature_level < 507:
                section.unsupported_fields.append(
                    "streams[].default_push_notifications"
                )
            elif any(
                isinstance(stream, dict)
                and "default_push_notifications" not in stream
                for stream in streams
            ):
                section.status = SectionStatus.PARTIAL
                section.absent_fields.append(
                    "streams[].default_push_notifications"
                )
        else:
            section.status = SectionStatus.PARTIAL
            section.warnings.append("Server did not report zulip_feature_level")
    return section


@contextmanager
def configuration_queue_snapshot() -> Iterator[ConfigurationQueueSnapshot]:
    response = configuration_request(
        "/register",
        method="POST",
        request={
            "event_types": [],
            "fetch_event_types": list(CONFIGURATION_FETCH_EVENT_TYPES),
        },
    )
    queue_id = response.get("queue_id")
    if not isinstance(queue_id, str) or not queue_id:
        raise ZulipAPIError(APIError(
            message="Zulip register response did not include a valid queue_id",
            code="INVALID_RESPONSE",
        ))

    snapshot = ConfigurationQueueSnapshot(data={})
    try:
        snapshot.data = {
            key: value for key, value in response.items() if key != "queue_id"
        }
        yield snapshot
    finally:
        try:
            configuration_request(
                "/events",
                method="DELETE",
                request={"queue_id": queue_id},
            )
        except ZulipAPIError as exc:
            if exc.error.code != "BAD_EVENT_QUEUE_ID":
                warning = f"Failed to delete configuration event queue: {exc.error.message}"
                snapshot.warnings.append(warning)
                _logger.warning("%s", warning)


def _failed_section(error: ZulipAPIError) -> SectionResult:
    def fail() -> dict[str, JSONValue]:
        raise error

    return read_section(fail)


def _queue_sections(
    snapshot: dict[str, JSONValue], requested: set[str],
) -> dict[str, SectionResult]:
    results: dict[str, SectionResult] = {}
    partitioned = partition_realm_snapshot(snapshot)
    for name in requested & QUEUE_SECTIONS:
        if name == "branding":
            results[name] = _branding_section(snapshot)
        elif name in partitioned:
            data = partitioned[name]
            results[name] = SectionResult(
                SectionStatus.OK if data else SectionStatus.EMPTY,
                data=data,
            )
        elif name == "new_user_defaults":
            field_name = "realm_user_settings_defaults"
            if field_name not in snapshot:
                feature_level = snapshot.get("zulip_feature_level")
                if isinstance(feature_level, int) and feature_level < 95:
                    results[name] = SectionResult(
                        SectionStatus.UNSUPPORTED,
                        unsupported_fields=[field_name],
                    )
                else:
                    results[name] = SectionResult(
                        SectionStatus.PARTIAL,
                        data={},
                        absent_fields=[field_name],
                        warnings=[f"Register response omitted {field_name}"],
                    )
            else:
                value = snapshot[field_name]
                if value is None:
                    results[name] = SectionResult(
                        SectionStatus.PARTIAL,
                        data=None,
                        warnings=[f"Register response returned null for {field_name}"],
                    )
                else:
                    results[name] = SectionResult(
                        SectionStatus.EMPTY if not value else SectionStatus.OK,
                        data=value,
                    )
        elif name == "default_channels":
            fields = ("default_streams", "default_stream_groups")
            present: dict[str, JSONValue] = {}
            missing = []
            for field in fields:
                if field in snapshot:
                    present[field] = snapshot[field]
                elif f"realm_{field}" in snapshot:
                    present[field] = snapshot[f"realm_{field}"]
                else:
                    missing.append(field)
            null_fields = [field for field, value in present.items() if value is None]
            if missing or null_fields:
                results[name] = SectionResult(
                    SectionStatus.PARTIAL,
                    data=present,
                    absent_fields=missing,
                    warnings=[
                        *(f"Register response omitted {field}" for field in missing),
                        *(f"Register response returned null for {field}" for field in null_fields),
                    ],
                )
            else:
                results[name] = SectionResult(
                    SectionStatus.OK if any(present.values()) else SectionStatus.EMPTY,
                    data=present,
                )
    return results


_BRANDING_ASSETS = {
    "icon": ("realm_icon_url", "realm_icon_source", "max_icon_file_size_mib"),
    "logo_light": ("realm_logo_url", "realm_logo_source", "max_logo_file_size_mib"),
    "logo_dark": (
        "realm_night_logo_url",
        "realm_night_logo_source",
        "max_logo_file_size_mib",
    ),
}


class _LocalFileError(ValueError):
    def __init__(self, message: str, code: str) -> None:
        self.code = code
        super().__init__(message)


def _branding_section(snapshot: dict[str, JSONValue]) -> SectionResult:
    data: dict[str, JSONValue] = {}
    absent: list[str] = []
    for asset, (url_field, source_field, size_field) in _BRANDING_ASSETS.items():
        item: dict[str, JSONValue] = {"asset": asset}
        for output, field in (
            ("url", url_field),
            ("source", source_field),
            ("max_file_size_mib", size_field),
        ):
            if field in snapshot:
                item[output] = snapshot[field]
            elif output != "max_file_size_mib":
                absent.append(field)
        data[asset] = item
    return SectionResult(
        SectionStatus.PARTIAL if absent else SectionStatus.OK,
        data=data,
        absent_fields=absent,
        warnings=[f"Register response omitted {field}" for field in absent],
    )


def get_organization_branding() -> SectionResult:
    try:
        snapshot = None
        with configuration_queue_snapshot() as snapshot:
            result = _branding_section(snapshot.data)
        result.warnings.extend(snapshot.warnings)
        return result
    except ZulipAPIError as exc:
        return _failed_section(exc)


def _add_bot_subscriptions(
    section: SectionResult,
    realm_wide_visibility: bool,
    users_response: dict[str, JSONValue] | None = None,
) -> None:
    if section.status not in {SectionStatus.OK, SectionStatus.PARTIAL}:
        return
    if not isinstance(section.data, dict):
        return
    bots = section.data.get("bots")
    if not isinstance(bots, list) or not bots:
        return

    members = users_response.get("members") if users_response is not None else None
    if isinstance(members, list):
        people = {
            user["user_id"]: user
            for user in members
            if isinstance(user, dict)
            and isinstance(user.get("user_id"), int)
            and not isinstance(user.get("user_id"), bool)
        }
        for bot in bots:
            if not isinstance(bot, dict):
                continue
            owner = people.get(bot.get("bot_owner_id"))
            if isinstance(owner, dict):
                bot["owner"] = {
                    key: owner[key]
                    for key in ("user_id", "email", "full_name")
                    if key in owner
                }

    active_bots = []
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        if bot.get("is_active") is False:
            section.status = SectionStatus.PARTIAL
            bot["subscription_status"] = "unsupported"
            bot["channel_subscriptions"] = None
            bot["subscription_errors"] = [{
                "status": "unsupported",
                "message": "Subscriber endpoints only report active users",
                "code": "INACTIVE_USER_SUBSCRIPTIONS_UNAVAILABLE",
                "http_status": None,
            }]
        else:
            active_bots.append(bot)
    if not active_bots:
        return

    try:
        stream_response = get_streams_configuration()
    except ZulipAPIError as exc:
        failure = _failed_section(exc)
        section.status = SectionStatus.PARTIAL
        section.warnings.append(f"Could not audit bot subscriptions: {exc.error.message}")
        for bot in active_bots:
            bot["subscription_status"] = failure.status.value
            bot["channel_subscriptions"] = []
            bot["subscription_errors"] = [exc.error.to_dict()]
        return

    streams = stream_response.get("streams")
    if not isinstance(streams, list):
        section.status = SectionStatus.PARTIAL
        section.warnings.append("Could not audit bot subscriptions: streams was absent or null")
        for bot in active_bots:
            bot["subscription_status"] = "error"
            bot["channel_subscriptions"] = []
            bot["subscription_errors"] = [{
                "message": "Streams were absent or null",
                "code": "INVALID_RESPONSE",
                "http_status": None,
            }]
        return

    stream_names = {
        stream["stream_id"]: stream["name"]
        for stream in streams
        if isinstance(stream, dict)
        and isinstance(stream.get("stream_id"), int)
        and not isinstance(stream.get("stream_id"), bool)
        and isinstance(stream.get("name"), str)
    }
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        for source, target in (
            ("default_sending_stream", "default_sending_channel"),
            ("default_events_register_stream", "default_events_register_channel"),
        ):
            stream_id = bot.get(source)
            if isinstance(stream_id, int) and stream_id in stream_names:
                bot[target] = stream_names[stream_id]

    bot_by_id = {
        bot["user_id"]: bot
        for bot in active_bots
        if isinstance(bot, dict)
        and isinstance(bot.get("user_id"), int)
        and not isinstance(bot.get("user_id"), bool)
    }
    for bot in bot_by_id.values():
        bot["subscription_status"] = "ok"
        bot["channel_subscriptions"] = []
        bot["subscription_errors"] = []

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_id = stream.get("stream_id")
        stream_name = stream.get("name")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            section.status = SectionStatus.PARTIAL
            section.warnings.append("Skipped channel without a valid stream_id")
            continue
        try:
            subscriber_response = get_channel_subscribers_configuration(stream_id)
        except ZulipAPIError as exc:
            failure = _failed_section(exc)
            section.status = SectionStatus.PARTIAL
            error = {
                "stream_id": stream_id,
                "stream_name": stream_name if isinstance(stream_name, str) else None,
                "status": failure.status.value,
                **exc.error.to_dict(),
            }
            for bot in bot_by_id.values():
                bot["subscription_status"] = "partial"
                errors = bot["subscription_errors"]
                assert isinstance(errors, list)
                errors.append(error)
            continue
        subscribers = subscriber_response.get("subscribers")
        if not isinstance(subscribers, list):
            section.status = SectionStatus.PARTIAL
            for bot in bot_by_id.values():
                bot["subscription_status"] = "partial"
                errors = bot["subscription_errors"]
                assert isinstance(errors, list)
                errors.append({
                    "stream_id": stream_id,
                    "stream_name": stream_name if isinstance(stream_name, str) else None,
                    "message": "Subscribers were absent or null",
                    "code": "INVALID_RESPONSE",
                    "http_status": None,
                })
            continue
        subscriber_ids = {
            user_id for user_id in subscribers
            if isinstance(user_id, int) and not isinstance(user_id, bool)
        }
        for user_id, bot in bot_by_id.items():
            if user_id in subscriber_ids:
                subscriptions = bot["channel_subscriptions"]
                assert isinstance(subscriptions, list)
                subscriptions.append({
                    "stream_id": stream_id,
                    "name": stream_name if isinstance(stream_name, str) else None,
                })

    if not realm_wide_visibility:
        section.status = SectionStatus.PARTIAL
        section.warnings.append(
            "Bot subscriptions may omit private channels hidden from the audit principal"
        )
        for bot in active_bots:
            if bot.get("subscription_status") == "ok":
                bot["subscription_status"] = "partial"
            errors = bot.get("subscription_errors")
            if isinstance(errors, list):
                errors.append({
                    "status": "partial",
                    "message": "Audit principal may not have realm-wide channel visibility",
                    "code": "CHANNEL_VISIBILITY_INCOMPLETE",
                    "http_status": None,
                })


def get_bots_audit(include_deactivated: bool = False) -> SectionResult:
    principal = read_section(get_current_user, CURRENT_USER_FIELDS)
    principal_data = principal.data
    realm_wide_visibility = (
        isinstance(principal_data, dict)
        and (
            principal_data.get("is_owner") is True
            or principal_data.get("is_admin") is True
        )
    )
    try:
        users_response = get_users_configuration()
    except ZulipAPIError as exc:
        return _failed_section(exc)
    result = read_section(
        lambda: project_bot_inventory(users_response, include_deactivated),
        collection_field="bots",
    )
    _add_bot_subscriptions(result, realm_wide_visibility, users_response)
    if principal.error is not None:
        result.status = SectionStatus.PARTIAL
        result.warnings.append(
            f"Could not establish channel visibility: {principal.error.message}"
        )
    return result


def get_organization_configuration(
    sections: list[str] | None = None,
    include_deactivated: bool = False,
    include_sensitive_user_fields: bool = False,
) -> dict[str, JSONValue]:
    requested_list = list(ORGANIZATION_SECTIONS) if sections is None else sections
    unknown = sorted(set(requested_list) - set(ORGANIZATION_SECTIONS))
    if unknown:
        raise ValueError(f"Unknown organization configuration sections: {', '.join(unknown)}")
    requested = set(requested_list)
    results: dict[str, SectionResult] = {}
    warnings: list[str] = []

    server = read_section(get_server_settings)
    principal = read_section(get_current_user, CURRENT_USER_FIELDS)

    if requested & QUEUE_SECTIONS:
        try:
            queue_snapshot = None
            with configuration_queue_snapshot() as queue_snapshot:
                results.update(_queue_sections(queue_snapshot.data, requested))
            warnings.extend(queue_snapshot.warnings)
        except ZulipAPIError as exc:
            for name in requested & QUEUE_SECTIONS:
                results[name] = _failed_section(exc)

    independent = {
        "groups": (get_user_groups_configuration, "user_groups"),
        "profile_fields": (get_profile_fields_configuration, "custom_fields"),
        "domains": (get_domains_configuration, "domains"),
        "linkifiers": (get_linkifiers_configuration, "linkifiers"),
        "emoji": (get_emoji_configuration, "emoji"),
        "invitations": (get_invitations_configuration, "invites"),
    }
    for name, (reader, collection_field) in independent.items():
        if name in requested:
            results[name] = read_section(reader, collection_field=collection_field)

    if requested & {"users", "bots"}:
        try:
            users_response = get_users_configuration()
        except ZulipAPIError as exc:
            if "users" in requested:
                results["users"] = _failed_section(exc)
            if "bots" in requested:
                results["bots"] = _failed_section(exc)
        else:
            if "users" in requested:
                results["users"] = read_section(
                    lambda: project_user_inventory(
                        users_response,
                        include_sensitive_user_fields,
                        include_deactivated,
                    ),
                    collection_field="members",
                )
            if "bots" in requested:
                results["bots"] = read_section(
                    lambda: project_bot_inventory(users_response, include_deactivated),
                    collection_field="bots",
                )
                principal_data = principal.data
                realm_wide_visibility = (
                    isinstance(principal_data, dict)
                    and (
                        principal_data.get("is_owner") is True
                        or principal_data.get("is_admin") is True
                    )
                )
                _add_bot_subscriptions(
                    results["bots"], realm_wide_visibility, users_response,
                )

    if "permissions" in results and "groups" in results:
        permissions = results["permissions"].data
        group_data = results["groups"].data
        if isinstance(permissions, dict) and isinstance(group_data, dict):
            group_list = group_data.get("user_groups")
            if isinstance(group_list, list):
                group_names = {
                    group["id"]: group["name"]
                    for group in group_list
                    if isinstance(group, dict)
                    and isinstance(group.get("id"), int)
                    and isinstance(group.get("name"), str)
                }
                permissions["resolved_group_settings"] = resolve_permission_groups(
                    permissions, group_names,
                )

    ordered_results = {
        name: results[name].to_dict() for name in requested_list if name in results
    }
    aggregate: dict[str, JSONValue] = {
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "principal": principal.data,
        "authority_status": {
            "server_settings": server.status.value,
            "principal": principal.status.value,
        },
        "authority": {
            "server_settings": server.to_dict(),
            "principal": principal.to_dict(),
        },
        "section_status": {
            name: result["status"] for name, result in ordered_results.items()
        },
        "sections": ordered_results,
        "warnings": warnings,
    }
    if isinstance(server.data, dict):
        for field_name in ("zulip_version", "zulip_feature_level"):
            if field_name in server.data:
                aggregate[field_name] = server.data[field_name]
    return aggregate


_admin_writes_enabled = False


def admin_writes_enabled() -> bool:
    """Return whether this server process currently permits admin writes."""
    return _admin_writes_enabled


def set_admin_writes_enabled(enabled: bool) -> None:
    """Set process-local admin write authorization."""
    global _admin_writes_enabled
    _admin_writes_enabled = enabled


def administrative_mutation_request(
    url: str,
    method: str,
    request: dict[str, JSONValue] | None = None,
    files: list[BinaryIO] | None = None,
) -> dict[str, JSONValue]:
    if not admin_writes_enabled():
        raise ZulipAPIError(APIError(
            message=(
                "Administrative writes are disabled; call "
                "enable_administrative_writes and confirm the request"
            ),
            code="ADMIN_WRITES_DISABLED",
        ))
    return configuration_request(url, method=method, request=request, files=files)


def _mutation_status(error: ZulipAPIError) -> MutationStatus:
    if error.error.code == "ADMIN_WRITES_DISABLED":
        return MutationStatus.DISABLED
    section = _failed_section(error)
    if section.status == SectionStatus.FORBIDDEN:
        return MutationStatus.FORBIDDEN
    if section.status == SectionStatus.UNSUPPORTED:
        return MutationStatus.UNSUPPORTED
    return MutationStatus.ERROR


def _mutation_failure(
    endpoint: str, dry_run: bool, error: ZulipAPIError,
) -> MutationResult:
    return MutationResult(
        status=_mutation_status(error),
        endpoint=endpoint,
        dry_run=dry_run,
        error=error.error,
    )


def _write_principal(
    endpoint: str, dry_run: bool,
) -> tuple[dict[str, JSONValue] | None, MutationResult | None]:
    try:
        principal = get_current_user()
    except ZulipAPIError as exc:
        return None, _mutation_failure(endpoint, dry_run, exc)
    if principal.get("is_owner") is not True and principal.get("is_admin") is not True:
        return None, MutationResult(
            status=MutationStatus.FORBIDDEN,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message="Administrative configuration writes require an owner or administrator",
                code="INSUFFICIENT_PERMISSION",
            ),
        )
    return principal, None


def _read_write_state(
    fields: set[str], defaults: bool,
) -> tuple[dict[str, JSONValue], list[str]]:
    with configuration_queue_snapshot() as snapshot:
        if defaults:
            raw = snapshot.data.get("realm_user_settings_defaults")
            current = dict(raw) if isinstance(raw, dict) else {}
        else:
            current = {
                field: snapshot.data[f"realm_{field}"]
                for field in fields
                if f"realm_{field}" in snapshot.data
            }
    return current, snapshot.warnings


def _invalid_fields_result(
    endpoint: str,
    dry_run: bool,
    fields: set[str],
    code: str = "INVALID_FIELD",
) -> MutationResult:
    names = ", ".join(sorted(fields))
    return MutationResult(
        status=MutationStatus.ERROR,
        endpoint=endpoint,
        dry_run=dry_run,
        error=APIError(message=f"Unsupported configuration fields: {names}", code=code),
    )


def _conflict_result(
    endpoint: str,
    dry_run: bool,
    current: dict[str, JSONValue],
    desired: dict[str, JSONValue],
    mismatches: list[str],
    warnings: list[str],
) -> MutationResult:
    return MutationResult(
        status=MutationStatus.CONFLICT,
        endpoint=endpoint,
        dry_run=dry_run,
        current=current,
        desired=desired,
        warnings=warnings,
        error=APIError(
            message=f"Expected current values did not match: {', '.join(mismatches)}",
            code="EXPECTED_VALUE_MISMATCH",
        ),
    )


def _named_id(
    value: JSONValue,
    named_ids: dict[str, int],
    kind: str,
) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{kind} references must use semantic names, not numeric IDs")
    if value in named_ids:
        return named_ids[value]
    matches = {
        item_id for name, item_id in named_ids.items()
        if name.casefold() == value.casefold()
    }
    if len(matches) == 1:
        return matches.pop()
    if len(matches) > 1:
        raise ValueError(f"Ambiguous {kind} name: {value}")
    raise ValueError(f"Unknown {kind} name: {value}")


def _resolve_group_setting(
    value: JSONValue,
    group_ids: dict[str, int],
    user_ids: dict[str, int],
) -> JSONValue:
    if isinstance(value, str):
        return _named_id(value, group_ids, "group")
    if not isinstance(value, dict):
        raise ValueError(
            "Group settings must use a group name or semantic direct_members/direct_subgroups"
        )
    extra = set(value) - {"direct_members", "direct_subgroups"}
    if extra:
        raise ValueError(f"Unknown anonymous group fields: {', '.join(sorted(extra))}")
    members = value.get("direct_members", [])
    subgroups = value.get("direct_subgroups", [])
    if not isinstance(members, list) or not isinstance(subgroups, list):
        raise ValueError("direct_members and direct_subgroups must be lists of names")
    return {
        "direct_members": [
            _named_id(member, user_ids, "user") for member in members
        ],
        "direct_subgroups": [
            _named_id(group, group_ids, "group") for group in subgroups
        ],
    }


def _semantic_name_maps() -> tuple[dict[str, int], dict[str, int]]:
    groups_response = get_user_groups_configuration()
    users_response = get_users_configuration()
    groups = groups_response.get("user_groups")
    users = users_response.get("members")
    if not isinstance(groups, list) or not isinstance(users, list):
        raise ValueError("User and group inventories are required for semantic resolution")
    group_ids = {
        group["name"]: group["id"]
        for group in groups
        if isinstance(group, dict)
        and isinstance(group.get("name"), str)
        and isinstance(group.get("id"), int)
        and not isinstance(group.get("id"), bool)
    }
    user_ids: dict[str, int] = {}
    duplicate_names: set[str] = set()
    for user in users:
        if (
            not isinstance(user, dict)
            or not isinstance(user.get("user_id"), int)
            or isinstance(user.get("user_id"), bool)
            or user.get("is_active") is False
        ):
            continue
        user_id = user["user_id"]
        email = user.get("email")
        if isinstance(email, str):
            user_ids[email] = user_id
        name = user.get("full_name")
        if isinstance(name, str):
            if name in user_ids and user_ids[name] != user_id:
                duplicate_names.add(name)
            else:
                user_ids[name] = user_id
    for name in duplicate_names:
        user_ids.pop(name, None)
    return group_ids, user_ids


def _channel_name_map() -> dict[str, int]:
    response = get_streams_configuration()
    streams = response.get("streams")
    if not isinstance(streams, list):
        raise ValueError("Channel inventory is required for semantic resolution")
    return {
        stream["name"]: stream["stream_id"]
        for stream in streams
        if isinstance(stream, dict)
        and isinstance(stream.get("name"), str)
        and isinstance(stream.get("stream_id"), int)
        and not isinstance(stream.get("stream_id"), bool)
    }


def _canonical_realm_value(field: str, value: JSONValue) -> JSONValue:
    if field in UNLIMITED_REALM_FIELDS:
        if value is None or value == -1 or value in ("forever", "unlimited"):
            return "unlimited"
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{field} must be an integer or 'unlimited'")
    if field in CHANNEL_REFERENCE_REALM_FIELDS and value in (None, -1):
        return -1
    if field in GROUP_SETTING_REALM_FIELDS and isinstance(value, dict):
        members = value.get("direct_members")
        subgroups = value.get("direct_subgroups")
        if isinstance(members, list) and isinstance(subgroups, list):
            return {
                "direct_members": sorted(members, key=repr),
                "direct_subgroups": sorted(subgroups, key=repr),
            }
    return value


def _realm_request_value(field: str, value: JSONValue) -> JSONValue:
    if field in UNLIMITED_REALM_FIELDS and value == "unlimited":
        return json.dumps(value)
    return value


def _resolve_realm_values(
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue],
) -> tuple[
    dict[str, JSONValue],
    dict[str, JSONValue],
    dict[str, JSONValue],
]:
    resolved_changes = dict(changes)
    resolved_expected = dict(expected)
    mappings: dict[str, JSONValue] = {}
    group_fields = (set(changes) | set(expected)) & GROUP_SETTING_REALM_FIELDS
    if group_fields:
        group_ids, user_ids = _semantic_name_maps()
        for field in sorted(group_fields):
            field_mapping: dict[str, JSONValue] = {}
            if field in changes:
                resolved = _resolve_group_setting(changes[field], group_ids, user_ids)
                resolved_changes[field] = resolved
                field_mapping["desired"] = {
                    "semantic": changes[field],
                    "resolved": resolved,
                }
            if field in expected:
                resolved = _resolve_group_setting(expected[field], group_ids, user_ids)
                resolved_expected[field] = resolved
                field_mapping["expected"] = {
                    "semantic": expected[field],
                    "resolved": resolved,
                }
            mappings[field] = field_mapping

    channel_fields = (set(changes) | set(expected)) & CHANNEL_REFERENCE_REALM_FIELDS
    if channel_fields:
        channel_ids = _channel_name_map()
        for field in sorted(channel_fields):
            field_mapping = {}
            if field in changes:
                value = changes[field]
                resolved = -1 if value is None else _named_id(value, channel_ids, "channel")
                resolved_changes[field] = resolved
                field_mapping["desired"] = {
                    "semantic": value,
                    "resolved": resolved,
                }
            if field in expected:
                value = expected[field]
                resolved = -1 if value is None else _named_id(value, channel_ids, "channel")
                resolved_expected[field] = resolved
                field_mapping["expected"] = {
                    "semantic": value,
                    "resolved": resolved,
                }
            mappings[field] = field_mapping

    for field, value in changes.items():
        if value is None and field not in (
            UNLIMITED_REALM_FIELDS | CHANNEL_REFERENCE_REALM_FIELDS
        ):
            raise ValueError(f"Null is not a supported write value for {field}")
        resolved_changes[field] = _canonical_realm_value(
            field, resolved_changes[field]
        )
    for field, value in expected.items():
        resolved_expected[field] = _canonical_realm_value(
            field, resolved_expected[field]
        )
    return resolved_changes, resolved_expected, mappings


def update_organization_configuration(
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm"
    expected = expected or {}
    requested_fields = set(changes) | set(expected)
    invalid = requested_fields - REALM_WRITE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(
        field for field, value in changes.items()
        if value is None
        and field not in (UNLIMITED_REALM_FIELDS | CHANNEL_REFERENCE_REALM_FIELDS)
    )
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_UNSUPPORTED",
            ),
        )

    principal, failure = _write_principal(endpoint, dry_run)
    if failure is not None:
        return failure
    assert principal is not None
    owner_only = set(changes) & OWNER_ONLY_REALM_FIELDS
    if owner_only and principal.get("is_owner") is not True:
        return MutationResult(
            status=MutationStatus.FORBIDDEN,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Owner authority is required for: {', '.join(sorted(owner_only))}",
                code="OWNER_REQUIRED",
            ),
        )
    authentication = changes.get("authentication_methods")
    if authentication is not None:
        if (
            not isinstance(authentication, dict)
            or not authentication
            or any(not isinstance(value, bool) for value in authentication.values())
            or not any(authentication.values())
        ):
            return MutationResult(
                status=MutationStatus.ERROR,
                endpoint=endpoint,
                dry_run=dry_run,
                desired=changes,
                error=APIError(
                    message="authentication_methods must enable at least one boolean method",
                    code="LAST_AUTHENTICATION_METHOD",
                ),
            )

    try:
        current, warnings = _read_write_state(requested_fields, defaults=False)
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    absent = requested_fields - set(current)
    if absent:
        result = _invalid_fields_result(endpoint, dry_run, absent, "UNSUPPORTED_FIELD")
        result.current = current
        result.desired = changes
        result.unsupported_fields = sorted(absent)
        result.warnings = warnings
        return result
    if isinstance(authentication, dict):
        current_authentication = current.get("authentication_methods")
        if not isinstance(current_authentication, dict):
            return MutationResult(
                status=MutationStatus.UNSUPPORTED,
                endpoint=endpoint,
                dry_run=dry_run,
                current=current,
                desired=changes,
                unsupported_fields=["authentication_methods"],
                warnings=warnings,
                error=APIError(
                    message="Current authentication methods were not available for lockout validation",
                    code="AUTHENTICATION_METHODS_UNAVAILABLE",
                ),
            )
        unknown_methods = set(authentication) - set(current_authentication)
        usable_methods = [
            method for method, enabled in authentication.items()
            if enabled is True and method in current_authentication
        ]
        if unknown_methods or not usable_methods:
            return MutationResult(
                status=MutationStatus.ERROR,
                endpoint=endpoint,
                dry_run=dry_run,
                current=current,
                desired=changes,
                warnings=warnings,
                error=APIError(
                    message=(
                        "Authentication update must leave a currently supported method enabled"
                    ),
                    code="LAST_AUTHENTICATION_METHOD",
                ),
            )
    try:
        resolved_changes, resolved_expected, mappings = _resolve_realm_values(
            changes, expected,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.warnings = warnings
        return result
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            current=current,
            desired=changes,
            warnings=warnings,
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    mismatches = sorted(
        field for field, value in resolved_expected.items()
        if _canonical_realm_value(field, current[field]) != value
    )
    if mismatches:
        result = _conflict_result(
            endpoint, dry_run, current, changes, mismatches, warnings,
        )
        result.resolved_mappings = mappings
        return result

    changed = sorted(
        field for field, value in resolved_changes.items()
        if _canonical_realm_value(field, current[field]) != value
    )
    request = {
        field: (
            {"new": resolved_changes[field], "old": current[field]}
            if field in GROUP_SETTING_REALM_FIELDS
            else _realm_request_value(field, resolved_changes[field])
        )
        for field in changed
    }
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request,
            changed_fields=changed,
            resolved_mappings=mappings,
            warnings=warnings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request={},
            readback=current,
            resolved_mappings=mappings,
            warnings=warnings + ["All requested fields already had the desired values"],
        )

    try:
        response = administrative_mutation_request(
            endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.changed_fields = changed
        result.resolved_mappings = mappings
        result.warnings = warnings
        return result
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = [value for value in ignored if isinstance(value, str)] if isinstance(ignored, list) else []
    try:
        readback, readback_warnings = _read_write_state(set(changes), defaults=False)
    except ZulipAPIError as exc:
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            unsupported_fields=unsupported,
            warnings=warnings,
            error=exc.error,
        )
    warnings.extend(readback_warnings)
    mismatched_readback = [
        field for field, value in resolved_changes.items()
        if field not in unsupported
        and (
            field not in readback
            or _canonical_realm_value(field, readback[field]) != value
        )
    ]
    if mismatched_readback:
        warnings.append(
            f"Readback did not match requested values: {', '.join(mismatched_readback)}"
        )
    status = (
        MutationStatus.PARTIAL
        if unsupported or mismatched_readback
        else MutationStatus.OK
    )
    return MutationResult(
        status=status,
        endpoint=endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=warnings,
    )


def update_default_user_settings(
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/user_settings_defaults"
    expected = expected or {}
    requested_fields = set(changes) | set(expected)
    invalid = requested_fields - DEFAULT_USER_WRITE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(field for field, value in changes.items() if value is None)
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_UNSUPPORTED",
            ),
        )
    _, failure = _write_principal(endpoint, dry_run)
    if failure is not None:
        return failure
    try:
        current, warnings = _read_write_state(requested_fields, defaults=True)
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    absent = requested_fields - set(current)
    if absent:
        result = _invalid_fields_result(endpoint, dry_run, absent, "UNSUPPORTED_FIELD")
        result.current = current
        result.desired = changes
        result.unsupported_fields = sorted(absent)
        result.warnings = warnings
        return result
    mismatches = sorted(
        field for field, value in expected.items() if current.get(field) != value
    )
    if mismatches:
        return _conflict_result(
            endpoint, dry_run, current, changes, mismatches, warnings,
        )
    changed = sorted(field for field, value in changes.items() if current[field] != value)
    request = {field: changes[field] for field in changed}
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request,
            changed_fields=changed,
            warnings=warnings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            readback=current,
            warnings=warnings + ["All requested fields already had the desired values"],
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.changed_fields = changed
        result.warnings = warnings
        return result
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = [value for value in ignored if isinstance(value, str)] if isinstance(ignored, list) else []
    try:
        readback, readback_warnings = _read_write_state(set(changes), defaults=True)
    except ZulipAPIError as exc:
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            unsupported_fields=unsupported,
            warnings=warnings,
            error=exc.error,
        )
    warnings.extend(readback_warnings)
    mismatched_readback = [
        field for field, value in changes.items()
        if field not in unsupported
        and (field not in readback or readback[field] != value)
    ]
    if mismatched_readback:
        warnings.append(
            f"Readback did not match requested values: {', '.join(mismatched_readback)}"
        )
    return MutationResult(
        status=(
            MutationStatus.PARTIAL
            if unsupported or mismatched_readback
            else MutationStatus.OK
        ),
        endpoint=endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        unsupported_fields=unsupported,
        warnings=warnings,
    )


def _admin_destination(
    endpoint: str, realm_url: str, dry_run: bool,
) -> tuple[dict[str, JSONValue] | None, dict[str, JSONValue] | None, MutationResult | None]:
    try:
        settings = get_server_settings()
    except ZulipAPIError as exc:
        return None, None, _mutation_failure(endpoint, dry_run, exc)
    actual_url = settings.get("realm_url")
    if not isinstance(actual_url, str):
        actual_url = settings.get("realm_uri")
    if not isinstance(actual_url, str):
        return None, None, MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message="Server settings did not identify the destination realm",
                code="REALM_URL_UNAVAILABLE",
            ),
        )
    if actual_url.rstrip("/") != realm_url.rstrip("/"):
        return None, None, MutationResult(
            status=MutationStatus.CONFLICT,
            endpoint=endpoint,
            dry_run=dry_run,
            current={"realm_url": actual_url},
            desired={"realm_url": realm_url},
            error=APIError(
                message="Explicit destination realm does not match this MCP server",
                code="DESTINATION_REALM_MISMATCH",
            ),
        )
    principal, failure = _write_principal(endpoint, dry_run)
    return settings, principal, failure


def _branding_asset(asset: str) -> tuple[str, str, str]:
    try:
        return _BRANDING_ASSETS[asset]
    except KeyError:
        raise ValueError(
            "asset must be one of: icon, logo_light, logo_dark"
        ) from None


def _detect_image(content: bytes) -> tuple[str, str]:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise _LocalFileError(
        "Branding file is not a supported PNG, JPEG, GIF, or WebP image",
        "INVALID_IMAGE",
    )


def _read_branding_file(
    file_path: str, max_file_size_mib: JSONValue = None,
) -> tuple[Path, bytes, str, str]:
    path = Path(file_path)
    if not path.is_absolute():
        raise _LocalFileError("file_path must be absolute", "LOCAL_FILE_INVALID")
    if path.is_symlink() or not path.is_file():
        raise _LocalFileError(
            "file_path must be an existing regular file, not a symlink",
            "LOCAL_FILE_INVALID",
        )
    content = path.read_bytes()
    media_type, extension = _detect_image(content)
    if (
        isinstance(max_file_size_mib, (int, float))
        and not isinstance(max_file_size_mib, bool)
        and len(content) > max_file_size_mib * 1024 * 1024
    ):
        raise _LocalFileError(
            f"Branding file exceeds the advertised {max_file_size_mib} MiB limit",
            "FILE_TOO_LARGE",
        )
    return path, content, media_type, extension


def download_organization_branding(
    asset: str, output_directory: str | None = None,
) -> SectionResult:
    try:
        _branding_asset(asset)
    except ValueError as exc:
        return SectionResult(
            SectionStatus.ERROR,
            error=APIError(message=str(exc), code="INVALID_ASSET"),
        )
    branding = get_organization_branding()
    if branding.status not in {SectionStatus.OK, SectionStatus.PARTIAL}:
        return branding
    assert isinstance(branding.data, dict)
    selected = branding.data.get(asset)
    if not isinstance(selected, dict) or not isinstance(selected.get("url"), str):
        return SectionResult(
            SectionStatus.UNSUPPORTED,
            error=APIError(
                message=f"Server did not report a URL for {asset}",
                code="BRANDING_URL_UNAVAILABLE",
            ),
        )
    try:
        content, _ = download_file(selected["url"])
        media_type, extension = _detect_image(content)
        if output_directory is None:
            directory = Path(tempfile.mkdtemp(prefix="zulipmcp-branding-"))
        else:
            directory = Path(output_directory)
            if not directory.is_absolute() or not directory.is_dir():
                raise ValueError("output_directory must be an absolute existing directory")
        descriptor, saved_name = tempfile.mkstemp(
            prefix=f"{asset}-", suffix=extension, dir=directory,
        )
        try:
            os.chmod(saved_name, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(content)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            Path(saved_name).unlink(missing_ok=True)
            raise
    except (OSError, ValueError) as exc:
        return SectionResult(
            SectionStatus.ERROR,
            error=APIError(message=str(exc), code="BRANDING_DOWNLOAD_FAILED"),
        )
    data: dict[str, JSONValue] = {
        "asset": asset,
        "path": str(Path(saved_name).resolve()),
        "byte_count": len(content),
        "media_type": media_type,
        "sha256": hashlib.sha256(content).hexdigest(),
        "url": selected["url"],
    }
    if "source" in selected:
        data["source"] = selected["source"]
    return SectionResult(SectionStatus.OK, data=data, warnings=branding.warnings)


def upload_organization_branding(
    realm_url: str,
    asset: str,
    file_path: str,
    expected_source: str | None = None,
    dry_run: bool = False,
) -> MutationResult:
    try:
        _branding_asset(asset)
    except ValueError as exc:
        return MutationResult(
            MutationStatus.ERROR, "/realm",
            dry_run=dry_run,
            error=APIError(message=str(exc), code="INVALID_ASSET"),
        )
    endpoint = "/realm/icon" if asset == "icon" else "/realm/logo"
    _, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    branding = get_organization_branding()
    if branding.status not in {SectionStatus.OK, SectionStatus.PARTIAL}:
        return MutationResult(
            MutationStatus.ERROR, endpoint, dry_run=dry_run,
            error=branding.error or APIError(
                message="Could not read current branding", code="BRANDING_READ_FAILED",
            ),
        )
    assert isinstance(branding.data, dict)
    current_asset = branding.data.get(asset)
    current = dict(current_asset) if isinstance(current_asset, dict) else {"asset": asset}
    if expected_source is not None and current.get("source") != expected_source:
        return MutationResult(
            MutationStatus.CONFLICT,
            endpoint,
            dry_run=dry_run,
            current=current,
            desired={"source": expected_source},
            error=APIError(
                message="Current branding source does not match expected_source",
                code="EXPECTED_VALUE_MISMATCH",
            ),
        )
    maximum = current.get("max_file_size_mib")
    try:
        path, content, media_type, _ = _read_branding_file(file_path, maximum)
    except (OSError, ValueError) as exc:
        return MutationResult(
            MutationStatus.ERROR, endpoint, dry_run=dry_run, current=current,
            error=APIError(
                message=str(exc),
                code=exc.code if isinstance(exc, _LocalFileError) else "LOCAL_FILE_INVALID",
            ),
        )
    summary: dict[str, JSONValue] = {
        "asset": asset,
        "file_path": str(path),
        "byte_count": len(content),
        "media_type": media_type,
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    request: dict[str, JSONValue] = {}
    if asset != "icon":
        request["night"] = asset == "logo_dark"
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN,
            endpoint,
            dry_run=True,
            current=current,
            desired=summary,
            request={**request, "file": summary},
            changed_fields=[asset],
            warnings=["Zulip transforms branding images; repeated uploads may differ"],
        )
    try:
        with path.open("rb") as upload:
            response = administrative_mutation_request(
                endpoint, "POST", request=request, files=[upload],
            )
    except (OSError, ZulipAPIError) as exc:
        if isinstance(exc, ZulipAPIError):
            result = _mutation_failure(endpoint, False, exc)
        else:
            result = MutationResult(
                MutationStatus.ERROR, endpoint, dry_run=False,
                error=APIError(message=str(exc), code="FILE_READ_FAILED"),
            )
        result.current = current
        result.desired = summary
        result.request = {**request, "file": summary}
        return result
    readback_section = get_organization_branding()
    readback_data = readback_section.data if isinstance(readback_section.data, dict) else {}
    selected = readback_data.get(asset) if isinstance(readback_data, dict) else None
    confirmed = (
        isinstance(selected, dict)
        and selected.get("source") == "U"
        and isinstance(selected.get("url"), str)
        and bool(selected["url"])
    )
    warnings = ["Zulip transforms branding images; repeated uploads may upload again"]
    if not confirmed:
        warnings.append("Upload succeeded but readback did not confirm uploaded branding")
    return MutationResult(
        MutationStatus.OK if confirmed else MutationStatus.PARTIAL,
        endpoint,
        dry_run=False,
        current=current,
        desired=summary,
        request={**request, "file": summary},
        response=response,
        readback=selected if isinstance(selected, dict) else None,
        changed_fields=[asset],
        warnings=warnings + readback_section.warnings,
    )


_BOT_UPDATE_FIELDS = frozenset({
    "full_name", "short_name", "owner", "default_sending_channel",
    "default_events_register_channel", "default_all_public_channels",
})


def _semantic_error(endpoint: str, dry_run: bool, message: str) -> MutationResult:
    return MutationResult(
        MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
        error=APIError(message=message, code="SEMANTIC_RESOLUTION_ERROR"),
    )


def _resolve_person(
    members: list[JSONValue], reference: str, *, bot: bool,
) -> dict[str, JSONValue]:
    if not isinstance(reference, str) or reference.isdigit():
        raise ValueError("Numeric user IDs are not accepted")
    candidates = [
        user for user in members
        if isinstance(user, dict)
        and user.get("is_bot") is bot
        and (
            str(user.get("email", "")).casefold() == reference.casefold()
            or user.get("full_name") == reference
        )
    ]
    if len(candidates) != 1:
        raise ValueError(f"Reference {reference!r} did not identify exactly one user")
    return candidates[0]


def _resolve_channels(
    streams: list[JSONValue], names: list[str],
) -> tuple[dict[str, int], list[dict[str, JSONValue]]]:
    if any(not isinstance(name, str) or name.isdigit() for name in names):
        raise ValueError("Numeric channel IDs are not accepted")
    mapping: dict[str, int] = {}
    resolved: list[dict[str, JSONValue]] = []
    for name in names:
        candidates = [
            stream for stream in streams
            if isinstance(stream, dict) and stream.get("name") == name
        ]
        if len(candidates) != 1:
            raise ValueError(f"Channel {name!r} did not resolve uniquely")
        stream_id = candidates[0].get("stream_id")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            raise ValueError(f"Channel {name!r} has no valid ID")
        mapping[name] = stream_id
        resolved.append({"semantic": name, "resolved": stream_id})
    return mapping, resolved


def _bot_public_state(bot: dict[str, JSONValue]) -> dict[str, JSONValue]:
    email = bot.get("email")
    short_name = None
    if isinstance(email, str):
        short_name = email.split("@", 1)[0].removesuffix("-bot")
    owner = bot.get("owner")
    return {
        "full_name": bot.get("full_name"),
        "short_name": short_name,
        "owner": owner.get("email") if isinstance(owner, dict) else None,
        "default_sending_channel": bot.get("default_sending_channel"),
        "default_events_register_channel": bot.get(
            "default_events_register_channel"
        ),
        "default_all_public_channels": bot.get("default_all_public_streams"),
    }


def _bot_audit_item(reference: str) -> tuple[SectionResult, dict[str, JSONValue] | None]:
    audit = get_bots_audit(include_deactivated=True)
    if not isinstance(audit.data, dict):
        return audit, None
    bots = audit.data.get("bots")
    if not isinstance(bots, list):
        return audit, None
    try:
        return audit, _resolve_person(bots, reference, bot=True)
    except ValueError:
        return audit, None


def update_bot_configuration(
    realm_url: str,
    bot: str,
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/bots"
    _, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    invalid = (set(changes) | set(expected or {})) - _BOT_UPDATE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    audit, item = _bot_audit_item(bot)
    if item is None:
        return _semantic_error(endpoint, dry_run, f"Bot {bot!r} did not resolve uniquely")
    bot_id = item.get("user_id")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool):
        return _semantic_error(endpoint, dry_run, "Bot has no valid user ID")
    endpoint = f"/bots/{bot_id}"
    users = get_users_configuration().get("members")
    streams = get_streams_configuration().get("streams")
    if not isinstance(users, list) or not isinstance(streams, list):
        return _semantic_error(endpoint, dry_run, "User or channel inventory unavailable")
    current = _bot_public_state(item)
    mappings: dict[str, JSONValue] = {"bot": {"semantic": bot, "resolved": bot_id}}
    request: dict[str, JSONValue] = {}
    desired = dict(changes)
    resolved_expected = dict(expected or {})
    field_map = {
        "full_name": "full_name", "short_name": "short_name",
        "default_all_public_channels": "default_all_public_streams",
    }
    try:
        for source, value in changes.items():
            if source == "owner":
                owner = _resolve_person(users, str(value), bot=False)
                if owner.get("is_active") is False:
                    raise ValueError("Bot owner must be active")
                request["bot_owner_id"] = owner["user_id"]
                desired[source] = owner.get("email")
                mappings["owner"] = {"semantic": value, "resolved": owner["user_id"]}
            elif source in {"default_sending_channel", "default_events_register_channel"}:
                target = source.replace("_channel", "_stream")
                if value in (None, ""):
                    request[target] = ""
                    desired[source] = None
                else:
                    channel_map, resolved = _resolve_channels(streams, [str(value)])
                    request[target] = channel_map[str(value)]
                    mappings[source] = resolved[0]
            else:
                request[field_map[source]] = value
    except (KeyError, ValueError) as exc:
        return _semantic_error(endpoint, dry_run, str(exc))
    try:
        if "owner" in resolved_expected:
            expected_owner = _resolve_person(
                users, str(resolved_expected["owner"]), bot=False,
            )
            resolved_expected["owner"] = expected_owner.get("email")
    except ValueError as exc:
        return _semantic_error(endpoint, dry_run, str(exc))
    for field, expected_value in resolved_expected.items():
        if current.get(field) != expected_value:
            return MutationResult(
                MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
                current=current, desired=desired, resolved_mappings=mappings,
                error=APIError(
                    message=f"Expected value did not match for {field}",
                    code="EXPECTED_VALUE_MISMATCH",
                ),
            )
    unchanged = [field for field, value in desired.items() if current.get(field) == value]
    for field in unchanged:
        upstream = {
            "owner": "bot_owner_id",
            "default_sending_channel": "default_sending_stream",
            "default_events_register_channel": "default_events_register_stream",
            **field_map,
        }[field]
        request.pop(upstream, None)
    changed = sorted(set(changes) - set(unchanged))
    if not request:
        return MutationResult(
            MutationStatus.OK, endpoint, dry_run=dry_run, current=current,
            desired=desired, request={}, resolved_mappings=mappings,
            warnings=["Bot already had the desired configuration"],
        )
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, endpoint, dry_run=True, current=current,
            desired=desired, request=request, changed_fields=changed,
            resolved_mappings=mappings,
        )
    try:
        response = administrative_mutation_request(endpoint, "PATCH", request)
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, False, exc)
        result.current, result.desired, result.request = current, desired, request
        return result
    _, readback_item = _bot_audit_item(bot)
    readback = _bot_public_state(readback_item) if readback_item is not None else None
    mismatch = readback is None or any(readback.get(k) != v for k, v in desired.items())
    return MutationResult(
        MutationStatus.PARTIAL if mismatch else MutationStatus.OK,
        endpoint, dry_run=False, current=current, desired=desired, request=request,
        response=response, readback=readback, changed_fields=changed,
        resolved_mappings=mappings,
        warnings=["Bot readback did not match requested configuration"] if mismatch else [],
    )


def set_bot_channel_subscriptions(
    realm_url: str,
    bot: str,
    channels: list[str],
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/users/me/subscriptions"
    _, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    audit, item = _bot_audit_item(bot)
    if item is None:
        return _semantic_error(endpoint, dry_run, f"Bot {bot!r} did not resolve uniquely")
    if audit.status != SectionStatus.OK or item.get("subscription_status") != "ok":
        return MutationResult(
            MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
            error=APIError(
                message="Exact subscriptions require complete channel visibility",
                code="CHANNEL_VISIBILITY_INCOMPLETE",
            ),
        )
    bot_id = item.get("user_id")
    streams = get_streams_configuration().get("streams")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or not isinstance(streams, list):
        return _semantic_error(endpoint, dry_run, "Bot or channel inventory unavailable")
    try:
        _, resolved = _resolve_channels(streams, channels)
    except ValueError as exc:
        return _semantic_error(endpoint, dry_run, str(exc))
    subscriptions = item.get("channel_subscriptions")
    subscription_entries = subscriptions if isinstance(subscriptions, list) else []
    current_names = {
        entry["name"] for entry in subscription_entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    desired_names = set(channels)
    additions = sorted(desired_names - current_names)
    removals = sorted(current_names - desired_names)
    request: dict[str, JSONValue] = {
        "to_subscribe": additions, "to_unsubscribe": removals,
        "principal": bot_id,
    }
    mappings: dict[str, JSONValue] = {
        "bot": {"semantic": bot, "resolved": bot_id}, "channels": resolved,
    }
    if not additions and not removals:
        return MutationResult(
            MutationStatus.OK, endpoint, dry_run=dry_run,
            current={"channels": sorted(current_names)},
            desired={"channels": sorted(desired_names)}, request=request,
            resolved_mappings=mappings,
            warnings=["Bot already had the exact requested subscriptions"],
        )
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, endpoint, dry_run=True,
            current={"channels": sorted(current_names)},
            desired={"channels": sorted(desired_names)}, request=request,
            changed_fields=["channel_subscriptions"], resolved_mappings=mappings,
        )
    completed_additions: list[str] = []
    completed_removals: list[str] = []
    try:
        if additions:
            administrative_mutation_request(
                endpoint, "POST", {
                    "subscriptions": [{"name": name} for name in additions],
                    "principals": [bot_id],
                    "authorization_errors_fatal": True,
                },
            )
            completed_additions = additions
        if removals:
            administrative_mutation_request(
                endpoint, "DELETE", {
                    "subscriptions": removals, "principals": [bot_id],
                },
            )
            completed_removals = removals
    except ZulipAPIError as exc:
        return MutationResult(
            MutationStatus.PARTIAL if completed_additions else _mutation_status(exc),
            endpoint, dry_run=False,
            current={"channels": sorted(current_names)},
            desired={"channels": sorted(desired_names)}, request=request,
            response={
                "subscribed": completed_additions,
                "unsubscribed": completed_removals,
                "remaining_subscriptions": [
                    name for name in additions if name not in completed_additions
                ],
                "remaining_unsubscriptions": [
                    name for name in removals if name not in completed_removals
                ],
            },
            changed_fields=["channel_subscriptions"], resolved_mappings=mappings,
            error=exc.error,
        )
    readback_audit, readback_item = _bot_audit_item(bot)
    readback_subs = (
        readback_item.get("channel_subscriptions")
        if readback_item is not None else None
    )
    readback_entries = readback_subs if isinstance(readback_subs, list) else []
    readback_names = {
        entry["name"] for entry in readback_entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    confirmed = readback_audit.status == SectionStatus.OK and readback_names == desired_names
    return MutationResult(
        MutationStatus.OK if confirmed else MutationStatus.PARTIAL,
        endpoint, dry_run=False,
        current={"channels": sorted(current_names)},
        desired={"channels": sorted(desired_names)}, request=request,
        response={"subscribed": additions, "unsubscribed": removals},
        readback={"channels": sorted(readback_names)},
        changed_fields=["channel_subscriptions"], resolved_mappings=mappings,
        warnings=[] if confirmed else ["Subscription readback was incomplete or mismatched"],
    )


def create_bot(
    realm_url: str,
    short_name: str,
    full_name: str,
    owner: str,
    bot_type: str = "generic",
    default_sending_channel: str | None = None,
    default_events_register_channel: str | None = None,
    default_all_public_channels: bool = False,
    channel_subscriptions: list[str] | None = None,
    avatar_path: str | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/bots"
    _, principal, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    if bot_type != "generic":
        return MutationResult(
            MutationStatus.ERROR, endpoint, dry_run=dry_run,
            error=APIError(message="Only generic bots are supported", code="INVALID_BOT_TYPE"),
        )
    if not short_name or short_name.isdigit():
        return _semantic_error(endpoint, dry_run, "short_name must be a non-numeric name")
    users_response = get_users_configuration()
    members = users_response.get("members")
    streams_response = get_streams_configuration()
    streams = streams_response.get("streams")
    if not isinstance(members, list) or not isinstance(streams, list):
        return _semantic_error(endpoint, dry_run, "User or channel inventory unavailable")
    try:
        owner_item = _resolve_person(members, owner, bot=False)
        if owner_item.get("is_active") is False:
            raise ValueError("Bot owner must be active")
        channel_names = [
            name for name in (
                default_sending_channel, default_events_register_channel,
            ) if name is not None
        ] + list(channel_subscriptions or [])
        channel_map, resolved_channels = _resolve_channels(streams, channel_names)
        avatar = None
        if avatar_path is not None:
            avatar = _read_branding_file(avatar_path)
    except _LocalFileError as exc:
        return MutationResult(
            MutationStatus.ERROR, endpoint, dry_run=dry_run,
            error=APIError(message=str(exc), code=exc.code),
        )
    except (KeyError, OSError, ValueError) as exc:
        return _semantic_error(endpoint, dry_run, str(exc))
    matching = []
    collision = []
    for member in members:
        if not isinstance(member, dict):
            continue
        email = member.get("email")
        local = email.split("@", 1)[0].removesuffix("-bot") if isinstance(email, str) else ""
        if local.casefold() == short_name.casefold():
            (matching if member.get("is_bot") is True else collision).append(member)
    if collision or len(matching) > 1:
        return MutationResult(
            MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
            error=APIError(
                message="Bot short name is ambiguous or collides with a non-bot user",
                code="BOT_IDENTITY_CONFLICT",
            ),
        )
    if matching:
        existing = matching[0]
        if existing.get("is_active") is False:
            return MutationResult(
                MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
                error=APIError(
                    message="Matching bot is deactivated",
                    code="BOT_ALREADY_DEACTIVATED",
                ),
            )
        reference = existing.get("email")
        assert isinstance(reference, str)
        changes: dict[str, JSONValue] = {
            "full_name": full_name,
            "short_name": short_name,
            "owner": owner,
            "default_sending_channel": default_sending_channel,
            "default_events_register_channel": default_events_register_channel,
            "default_all_public_channels": default_all_public_channels,
        }
        result = update_bot_configuration(
            realm_url, reference, changes, dry_run=dry_run,
        )
        result.warnings.append("Converged an existing active bot; no bot was created")
        if channel_subscriptions is not None and result.status in {
            MutationStatus.OK, MutationStatus.DRY_RUN,
        }:
            subscriptions_result = set_bot_channel_subscriptions(
                realm_url, reference, channel_subscriptions, dry_run,
            )
            result.status = subscriptions_result.status
            result.resolved_mappings["channel_subscriptions"] = (
                subscriptions_result.resolved_mappings.get("channels", [])
            )
            result.warnings.extend(subscriptions_result.warnings)
        return result
    request: dict[str, JSONValue] = {
        "short_name": short_name,
        "full_name": full_name,
        "bot_type": 1,
        "default_all_public_streams": default_all_public_channels,
    }
    if default_sending_channel is not None:
        request["default_sending_stream"] = channel_map[default_sending_channel]
    if default_events_register_channel is not None:
        request["default_events_register_stream"] = channel_map[
            default_events_register_channel
        ]
    mappings: dict[str, JSONValue] = {
        "owner": {"semantic": owner, "resolved": owner_item.get("user_id")},
        "channels": resolved_channels,
    }
    desired: dict[str, JSONValue] = {
        "short_name": short_name, "full_name": full_name,
        "owner": owner_item.get("email"),
        "bot_type": bot_type,
        "default_sending_channel": default_sending_channel,
        "default_events_register_channel": default_events_register_channel,
        "default_all_public_channels": default_all_public_channels,
        "channel_subscriptions": channel_subscriptions or [],
    }
    if avatar is not None:
        _, content, media_type, _ = avatar
        request["avatar"] = {
            "file_path": avatar_path, "byte_count": len(content),
            "media_type": media_type, "sha256": hashlib.sha256(content).hexdigest(),
        }
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, endpoint, dry_run=True,
            desired=desired, request=request, changed_fields=["bot"],
            resolved_mappings=mappings,
        )
    try:
        if avatar is None:
            response = administrative_mutation_request(endpoint, "POST", request)
        else:
            avatar_file = avatar[0]
            wire_request = {key: value for key, value in request.items() if key != "avatar"}
            with avatar_file.open("rb") as upload:
                response = administrative_mutation_request(
                    endpoint, "POST", wire_request, files=[upload],
                )
        response.pop("api_key", None)
    except (OSError, ZulipAPIError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="FILE_READ_FAILED",
        )
        return MutationResult(
            _mutation_status(exc) if isinstance(exc, ZulipAPIError) else MutationStatus.ERROR,
            endpoint, dry_run=False, desired=desired, request=request,
            resolved_mappings=mappings, error=error,
        )
    bot_id = response.get("user_id")
    bot_email = response.get("email")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool):
        return MutationResult(
            MutationStatus.PARTIAL, endpoint, dry_run=False,
            desired=desired, request=request,
            response=response, changed_fields=["bot"], resolved_mappings=mappings,
            warnings=["Bot was created but the response omitted its user ID"],
        )
    warnings: list[str] = []
    if isinstance(principal, dict) and principal.get("user_id") != owner_item.get("user_id"):
        try:
            administrative_mutation_request(
                f"/bots/{bot_id}", "PATCH", {"bot_owner_id": owner_item["user_id"]},
            )
        except ZulipAPIError as exc:
            warnings.append(f"Bot was created but owner update failed: {exc.error.message}")
    if channel_subscriptions is not None:
        if not isinstance(bot_email, str):
            warnings.append("Bot was created but subscriptions could not resolve its email")
        else:
            sub_result = set_bot_channel_subscriptions(
                realm_url, bot_email, channel_subscriptions, False,
            )
            if sub_result.status != MutationStatus.OK:
                warnings.append("Bot was created but exact subscriptions were not completed")
    readback = None
    if isinstance(bot_email, str):
        _, readback_item = _bot_audit_item(bot_email)
        if readback_item is not None:
            readback = _bot_public_state(readback_item)
            expected_readback = {
                key: desired[key] for key in (
                    "full_name", "short_name", "owner",
                    "default_sending_channel",
                    "default_events_register_channel",
                    "default_all_public_channels",
                )
            }
            if any(readback.get(key) != value for key, value in expected_readback.items()):
                warnings.append("Bot was created but configuration readback mismatched")
        else:
            warnings.append("Bot was created but authoritative readback failed")
    else:
        warnings.append("Bot was created but its email was unavailable for readback")
    return MutationResult(
        MutationStatus.PARTIAL if warnings else MutationStatus.OK,
        endpoint, dry_run=False, desired=desired, request=request, response=response,
        readback=readback, changed_fields=["bot"],
        resolved_mappings=mappings, warnings=warnings,
    )


def _channel_inventory() -> list[dict[str, JSONValue]]:
    response = get_streams_configuration(
        include_all=True,
        include_default=True,
        exclude_archived=False,
    )
    streams = response.get("streams")
    if not isinstance(streams, list):
        raise ValueError("Channel inventory was absent or null")
    return [stream for stream in streams if isinstance(stream, dict)]


def _find_channel(
    streams: list[dict[str, JSONValue]], name: str,
) -> dict[str, JSONValue] | None:
    exact = [stream for stream in streams if stream.get("name") == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"Ambiguous channel name: {name}")
    folded = [
        stream for stream in streams
        if isinstance(stream.get("name"), str)
        and stream["name"].casefold() == name.casefold()
    ]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise ValueError(f"Ambiguous channel name: {name}")
    return None


def _channel_privacy(channel: dict[str, JSONValue]) -> str:
    if channel.get("invite_only") is True:
        return "private"
    if channel.get("is_web_public") is True:
        return "web_public"
    return "public"


def _channel_value(field: str, value: JSONValue) -> JSONValue:
    if field == "message_retention_days":
        if value is None or value == "realm_default":
            return "realm_default"
        if value == -1 or value in ("forever", "unlimited"):
            return "unlimited"
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("message_retention_days must be an integer, realm_default, or unlimited")
    if field in CHANNEL_GROUP_FIELDS and isinstance(value, dict):
        members = value.get("direct_members")
        subgroups = value.get("direct_subgroups")
        if isinstance(members, list) and isinstance(subgroups, list):
            return {
                "direct_members": sorted(members, key=repr),
                "direct_subgroups": sorted(subgroups, key=repr),
            }
    return value


def _channel_current(channel: dict[str, JSONValue], field: str) -> tuple[bool, JSONValue]:
    if field == "privacy":
        return True, _channel_privacy(channel)
    if field == "new_name":
        return "name" in channel, channel.get("name")
    if field == "is_default_stream":
        return "is_default" in channel, channel.get("is_default")
    return field in channel, _channel_value(field, channel.get(field))


def _channel_impact(
    channel: dict[str, JSONValue],
) -> tuple[dict[str, JSONValue], list[str]]:
    stream_id = channel.get("stream_id")
    if not isinstance(stream_id, int) or isinstance(stream_id, bool):
        raise ValueError("Target channel did not have a valid stream_id")
    warnings = [
        "Archival retains all channel messages and hides them with the channel",
        "Zulip does not expose an authoritative message count without reading messages",
    ]
    impact: dict[str, JSONValue] = {
        "channel": {
            key: channel[key]
            for key in (
                "name", "stream_id", "description", "is_archived", "is_default",
                "invite_only", "is_web_public", "message_retention_days",
                "topics_policy", "folder_id",
                *sorted(CHANNEL_GROUP_FIELDS),
            )
            if key in channel
        },
        "privacy": _channel_privacy(channel),
        "messages_retained": True,
    }
    if "first_message_id" in channel:
        impact["message_history_present"] = channel.get("first_message_id") is not None
    else:
        warnings.append("Message presence unavailable: channel omitted first_message_id")
    try:
        subscribers = get_channel_subscribers_configuration(stream_id).get("subscribers")
        if not isinstance(subscribers, list):
            raise ValueError("Subscriber inventory was absent or null")
        subscriber_ids = {
            user_id for user_id in subscribers
            if isinstance(user_id, int) and not isinstance(user_id, bool)
        }
        members = get_users_configuration().get("members")
        if not isinstance(members, list):
            raise ValueError("User inventory was absent or null")
        identities = [
            {
                key: user[key]
                for key in ("user_id", "email", "full_name")
                if key in user
            }
            for user in members
            if isinstance(user, dict) and user.get("user_id") in subscriber_ids
        ]
        impact["subscriber_count"] = len(subscriber_ids)
        impact["visible_subscribers"] = identities
    except (ValueError, ZulipAPIError) as exc:
        message = exc.error.message if isinstance(exc, ZulipAPIError) else str(exc)
        warnings.append(f"Subscriber impact unavailable: {message}")
    try:
        topics_response = configuration_request(f"/users/me/{stream_id}/topics")
        topics = topics_response.get("topics")
        if isinstance(topics, list):
            impact["topic_count"] = len(topics)
        else:
            warnings.append("Topic count unavailable: response omitted topics")
    except ZulipAPIError as exc:
        warnings.append(f"Topic count unavailable: {exc.error.message}")
    try:
        references, queue_warnings = _read_write_state(
            set(CHANNEL_REFERENCE_REALM_FIELDS), defaults=False,
        )
        impact["realm_references"] = {
            field: value for field, value in references.items()
            if value == stream_id
        }
        impact["realm_references_complete"] = True
        warnings.extend(queue_warnings)
    except ZulipAPIError as exc:
        impact["realm_references"] = {}
        impact["realm_references_complete"] = False
        warnings.append(f"Realm channel references unavailable: {exc.error.message}")
    folder_id = channel.get("folder_id")
    if "folder_id" in channel:
        impact["channel_folder_membership"] = (
            {"folder_id": folder_id} if folder_id is not None else []
        )
        if folder_id is not None:
            warnings.append("Channel folder name is unavailable; reporting its realm-local ID")
    else:
        warnings.append("Channel folder membership unavailable")
    return impact, warnings


def set_channel_archived(
    realm_url: str,
    channel: str,
    archived: bool,
    expected_archived: bool | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/streams/{stream_id}"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    feature_level = server.get("zulip_feature_level")
    if not isinstance(feature_level, int) or feature_level < 315:
        return MutationResult(
            MutationStatus.UNSUPPORTED, endpoint, dry_run=dry_run,
            desired={"is_archived": archived},
            error=APIError(
                message="Archived channel inventory requires Zulip feature level 315",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    if not archived and feature_level < 388:
        return MutationResult(
            MutationStatus.UNSUPPORTED, endpoint, dry_run=dry_run,
            desired={"is_archived": False},
            error=APIError(
                message="Channel unarchiving requires Zulip feature level 388",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    if not isinstance(channel, str) or channel.isdigit():
        return _semantic_error(endpoint, dry_run, "Numeric channel IDs are not accepted")
    try:
        current_channel = _find_channel(_channel_inventory(), channel)
        if current_channel is None:
            raise ValueError(f"Unknown channel name: {channel}")
        stream_id = current_channel.get("stream_id")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            raise ValueError("Target channel did not have a valid stream_id")
        if "is_archived" not in current_channel:
            return MutationResult(
                MutationStatus.UNSUPPORTED, endpoint, dry_run=dry_run,
                current=current_channel,
                error=APIError(
                    message="Channel inventory omitted is_archived",
                    code="UNSUPPORTED_FIELD",
                ),
            )
        impact, warnings = _channel_impact(current_channel)
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return _semantic_error(endpoint, dry_run, str(exc))
    current_archived = current_channel["is_archived"] is True
    resolved_endpoint = f"/streams/{stream_id}"
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "channel": {"semantic": channel, "resolved": stream_id},
    }
    current = {"is_archived": current_archived, "impact": impact}
    desired = {"is_archived": archived}
    if expected_archived is not None and expected_archived != current_archived:
        return MutationResult(
            MutationStatus.CONFLICT, resolved_endpoint, dry_run=dry_run,
            current=current, desired=desired, resolved_mappings=mappings,
            warnings=warnings,
            error=APIError(
                message="Channel archive state did not match expected_archived",
                code="EXPECTED_VALUE_MISMATCH",
            ),
        )
    references = impact.get("realm_references")
    blockers = []
    if archived and current_channel.get("is_default") is True:
        blockers.append("channel is still a default channel")
    if archived and isinstance(references, dict) and references:
        blockers.append("realm settings still reference the channel")
    if archived and impact.get("realm_references_complete") is not True:
        blockers.append("realm channel references could not be audited")
    if blockers:
        return MutationResult(
            MutationStatus.CONFLICT, resolved_endpoint, dry_run=dry_run,
            current=current, desired=desired, resolved_mappings=mappings,
            warnings=warnings,
            error=APIError(
                message="; ".join(blockers), code="CHANNEL_ARCHIVE_BLOCKED",
            ),
        )
    if current_archived == archived:
        return MutationResult(
            MutationStatus.OK, resolved_endpoint, dry_run=dry_run,
            current=current, desired=desired, readback={"is_archived": archived},
            resolved_mappings=mappings,
            warnings=warnings + ["Channel already had the requested archive state"],
        )
    method = "DELETE" if archived else "PATCH"
    request: dict[str, JSONValue] = {} if archived else {"is_archived": False}
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, resolved_endpoint, dry_run=True,
            current=current, desired=desired, request=request,
            changed_fields=["is_archived"], resolved_mappings=mappings,
            warnings=warnings,
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method=method, request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, False, exc)
        result.current, result.desired, result.request = current, desired, request
        result.resolved_mappings, result.warnings = mappings, warnings
        return result
    try:
        readback_channel = next(
            (item for item in _channel_inventory() if item.get("stream_id") == stream_id),
            None,
        )
    except (ValueError, ZulipAPIError) as exc:
        readback_channel = None
        message = exc.error.message if isinstance(exc, ZulipAPIError) else str(exc)
        warnings.append(f"Archive readback failed: {message}")
    confirmed = (
        isinstance(readback_channel, dict)
        and readback_channel.get("is_archived") is archived
    )
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = [value for value in ignored if isinstance(value, str)] \
        if isinstance(ignored, list) else []
    if not confirmed:
        warnings.append("Archive readback did not confirm the requested state")
    return MutationResult(
        MutationStatus.OK if confirmed and not unsupported else MutationStatus.PARTIAL,
        resolved_endpoint, dry_run=False, current=current, desired=desired,
        request=request, response=response,
        readback={"is_archived": readback_channel.get("is_archived")} \
            if isinstance(readback_channel, dict) else None,
        changed_fields=["is_archived"], resolved_mappings=mappings,
        unsupported_fields=unsupported, warnings=warnings,
    )


def _resolve_channel_inputs(
    values: dict[str, JSONValue],
    group_ids: dict[str, int],
    user_ids: dict[str, int],
) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    resolved = dict(values)
    mappings: dict[str, JSONValue] = {}
    for field in sorted(set(values) & CHANNEL_GROUP_FIELDS):
        setting = _resolve_group_setting(values[field], group_ids, user_ids)
        resolved[field] = setting
        mappings[field] = {
            "semantic": values[field],
            "resolved": setting,
        }
    for field, value in list(resolved.items()):
        resolved[field] = _channel_value(field, value)
    return resolved, mappings


def _channel_write_value(field: str, value: JSONValue) -> JSONValue:
    if field == "message_retention_days" and value in {
        "realm_default", "unlimited",
    }:
        return json.dumps(value)
    return value


def _channel_configuration_delta(
    channel: dict[str, JSONValue],
    desired: dict[str, JSONValue],
) -> tuple[
    dict[str, JSONValue],
    list[str],
    list[str],
    dict[str, JSONValue],
]:
    current: dict[str, JSONValue] = {}
    changed: list[str] = []
    absent: list[str] = []
    request: dict[str, JSONValue] = {}
    for field, desired_value in desired.items():
        present, current_value = _channel_current(channel, field)
        if not present:
            absent.append(field)
            continue
        current[field] = current_value
        if _channel_value(field, current_value) == _channel_value(
            field, desired_value,
        ):
            continue
        changed.append(field)
        if field in CHANNEL_GROUP_FIELDS:
            request[field] = {"new": desired_value, "old": current_value}
        elif field == "privacy":
            request["is_private"] = desired_value == "private"
            request["is_web_public"] = desired_value == "web_public"
        else:
            request[field] = _channel_write_value(field, desired_value)
    if (
        "privacy" in changed
        and "history_public_to_subscribers" in desired
        and "history_public_to_subscribers" not in absent
    ):
        request["history_public_to_subscribers"] = desired[
            "history_public_to_subscribers"
        ]
    return current, sorted(changed), sorted(absent), request


def _channel_subscriber_ids(stream_id: int) -> set[int]:
    response = get_channel_subscribers_configuration(stream_id)
    subscribers = response.get("subscribers")
    if not isinstance(subscribers, list):
        raise ValueError("Channel subscriber readback was absent or null")
    return {
        user_id for user_id in subscribers
        if isinstance(user_id, int) and not isinstance(user_id, bool)
    }


def _channel_step(
    name: str,
    method: str,
    endpoint: str,
    request: dict[str, JSONValue],
    status: str,
    response: dict[str, JSONValue] | None = None,
    error: APIError | None = None,
) -> dict[str, JSONValue]:
    step: dict[str, JSONValue] = {
        "name": name,
        "method": method,
        "endpoint": endpoint,
        "request": request,
        "status": status,
    }
    if response is not None:
        step["response"] = response
    if error is not None:
        step["error"] = error.to_dict()
    return step


def create_channel(
    realm_url: str,
    name: str,
    subscribers: list[str],
    privacy: str,
    permissions: dict[str, JSONValue],
    description: str = "",
    settings: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
    is_default: bool | None = None,
) -> MutationResult:
    endpoint = "/channels/create"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    feature_level = server.get("zulip_feature_level")
    if not isinstance(feature_level, int) or feature_level < 417:
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message="Dedicated channel creation requires Zulip feature level 417",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    if privacy not in {"public", "private", "web_public"}:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(message="privacy must be public, private, or web_public", code="INVALID_PRIVACY"),
        )
    requested_settings = dict(settings or {})
    if is_default is not None:
        if not isinstance(is_default, bool):
            return MutationResult(
                status=MutationStatus.ERROR,
                endpoint=endpoint,
                dry_run=dry_run,
                error=APIError(
                    message="is_default must be a boolean",
                    code="INVALID_FIELD_VALUE",
                ),
            )
        previous = requested_settings.get("is_default_stream")
        if "is_default_stream" in requested_settings and previous != is_default:
            return MutationResult(
                status=MutationStatus.CONFLICT,
                endpoint=endpoint,
                dry_run=dry_run,
                desired={
                    "is_default": is_default,
                    "is_default_stream": previous,
                },
                error=APIError(
                    message=(
                        "is_default conflicts with settings.is_default_stream"
                    ),
                    code="CONFLICTING_FIELD_ALIASES",
                ),
            )
        requested_settings["is_default_stream"] = is_default
    if requested_settings.get("is_default_stream") is True and privacy == "private":
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"name": name, "privacy": privacy, **requested_settings},
            error=APIError(
                message="A private channel cannot be a default channel",
                code="INVALID_DEFAULT_CHANNEL",
            ),
        )
    invalid_permissions = set(permissions) - CHANNEL_GROUP_FIELDS
    invalid_settings = set(requested_settings) - CHANNEL_CREATE_FIELDS
    if invalid_permissions or invalid_settings:
        return _invalid_fields_result(
            endpoint, dry_run, invalid_permissions | invalid_settings,
        )
    null_fields = sorted(
        field for field, value in {**permissions, **requested_settings}.items()
        if value is None
    )
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"name": name},
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    try:
        streams = _channel_inventory()
        group_ids, user_ids = _semantic_name_maps()
        subscriber_ids = [_named_id(user, user_ids, "user") for user in subscribers]
        resolved_permissions, permission_mappings = _resolve_channel_inputs(
            permissions, group_ids, user_ids,
        )
        resolved_settings, setting_mappings = _resolve_channel_inputs(
            requested_settings, group_ids, user_ids,
        )
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"name": name},
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "subscribers": [
            {"semantic": user, "resolved": user_id}
            for user, user_id in zip(subscribers, subscriber_ids)
        ],
        **permission_mappings,
        **setting_mappings,
    }
    desired = {
        "name": name,
        "description": description,
        "privacy": privacy,
        "subscribers": subscribers,
        "permissions": permissions,
        **requested_settings,
    }
    desired_configuration: dict[str, JSONValue] = {
        "description": description,
        "privacy": privacy,
        **resolved_permissions,
        **{
            field: value for field, value in resolved_settings.items()
            if field != "announce"
        },
    }
    creation_permissions = {
        field: value for field, value in resolved_permissions.items()
        if field not in CHANNEL_CREATE_RESIDUAL_FIELDS
    }
    request: dict[str, JSONValue] = {
        "name": name,
        "description": description,
        "subscribers": subscriber_ids,
        "invite_only": privacy == "private",
        "is_web_public": privacy == "web_public",
        **{
            field: _channel_write_value(field, value)
            for field, value in resolved_settings.items()
        },
        **creation_permissions,
    }
    residual_template = {
        field: {"new": value, "old": "<creation-readback>"}
        for field, value in resolved_permissions.items()
        if field in CHANNEL_CREATE_RESIDUAL_FIELDS
    }
    existing = _find_channel(streams, name)
    steps: list[dict[str, JSONValue]] = []
    if existing is None and dry_run:
        steps.append(_channel_step(
            "create", "POST", endpoint, request, "planned",
        ))
        if residual_template:
            steps.append(_channel_step(
                "configure", "PATCH", "/streams/{new_stream_id}",
                residual_template, "conditional_on_readback",
            ))
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            desired=desired,
            request=request,
            changed_fields=sorted(request),
            resolved_mappings=mappings,
            steps=steps,
            remaining_fields=sorted(
                set(desired_configuration)
                | ({"subscribers"} if subscribers else set())
            ),
        )

    responses: dict[str, JSONValue] = {}
    unsupported: list[str] = []
    warnings: list[str] = []
    readback = existing
    was_existing = existing is not None
    if readback is None:
        try:
            create_response = administrative_mutation_request(
                endpoint, method="POST", request=request,
            )
        except ZulipAPIError as exc:
            steps.append(_channel_step(
                "create", "POST", endpoint, request, "failed",
                error=exc.error,
            ))
            result = _mutation_failure(endpoint, False, exc)
            result.desired = desired
            result.request = request
            result.resolved_mappings = mappings
            result.steps = steps
            result.remaining_fields = sorted(desired_configuration)
            result.failed_step = "create"
            return result
        responses["create"] = create_response
        steps.append(_channel_step(
            "create", "POST", endpoint, request, "ok",
            response=create_response,
        ))
        ignored = create_response.get("ignored_parameters_unsupported")
        if isinstance(ignored, list):
            unsupported.extend(
                value for value in ignored if isinstance(value, str)
            )
        try:
            readback = _find_channel(_channel_inventory(), name)
        except (ZulipAPIError, ValueError) as exc:
            error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
                message=str(exc), code="READBACK_ERROR",
            )
            return MutationResult(
                MutationStatus.PARTIAL, endpoint, dry_run=False,
                desired=desired, request=request, response=responses,
                resolved_mappings=mappings, error=error, steps=steps,
                remaining_fields=sorted(
                    set(desired_configuration)
                    | ({"subscribers"} if subscribers else set())
                ),
                failed_step="creation_readback",
            )
        if readback is None:
            return MutationResult(
                MutationStatus.PARTIAL, endpoint, dry_run=False,
                desired=desired, request=request, response=responses,
                resolved_mappings=mappings,
                error=APIError(
                    message="Created channel was absent from authoritative readback",
                    code="READBACK_ERROR",
                ),
                steps=steps,
                remaining_fields=sorted(
                    set(desired_configuration)
                    | ({"subscribers"} if subscribers else set())
                ),
                failed_step="creation_readback",
            )
    stream_id = readback.get("stream_id")
    if not isinstance(stream_id, int) or isinstance(stream_id, bool):
        return MutationResult(
            MutationStatus.PARTIAL, endpoint, dry_run=dry_run,
            current=readback, desired=desired, request=request,
            response=responses or None, readback=readback,
            resolved_mappings=mappings,
            error=APIError(
                message="Channel readback did not contain a valid stream_id",
                code="READBACK_ERROR",
            ),
            steps=steps,
            remaining_fields=sorted(
                set(desired_configuration)
                | ({"subscribers"} if subscribers else set())
            ),
            failed_step="creation_readback",
        )
    mappings["channel"] = {"semantic": name, "resolved": stream_id}
    resolved_endpoint = f"/streams/{stream_id}"
    current, changed, absent, update_request = _channel_configuration_delta(
        readback, desired_configuration,
    )
    if absent:
        return MutationResult(
            MutationStatus.PARTIAL, resolved_endpoint, dry_run=dry_run,
            current=current, desired=desired, request=request,
            response=responses or None, readback=readback,
            resolved_mappings=mappings, unsupported_fields=absent,
            warnings=warnings,
            error=APIError(
                message="Channel readback omitted fields required for convergence: "
                + ", ".join(absent),
                code="UNSUPPORTED_FIELD",
            ),
            steps=steps, remaining_fields=sorted(
                set(changed)
                | set(absent)
                | ({"subscribers"} if subscribers else set())
            ),
            failed_step="creation_readback",
        )
    try:
        current_subscribers = (
            _channel_subscriber_ids(stream_id) if subscribers else set()
        )
    except (ValueError, ZulipAPIError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            MutationStatus.PARTIAL, resolved_endpoint, dry_run=dry_run,
            current=current, desired=desired, request=request,
            response=responses or None, readback=readback,
            changed_fields=changed, resolved_mappings=mappings,
            warnings=warnings, error=error, steps=steps,
            remaining_fields=sorted(set(changed) | {"subscribers"}),
            failed_step="subscriber_readback",
        )
    missing_subscriber_ids = sorted(set(subscriber_ids) - current_subscribers)
    if was_existing:
        warnings.append(
            "Channel already exists; converging requested configuration"
            if changed or missing_subscriber_ids
            else "Channel already exists with the requested configuration"
        )
    subscribe_request: dict[str, JSONValue] = {
        "subscriptions": [{"name": name}],
        "principals": missing_subscriber_ids,
        "authorization_errors_fatal": True,
    }
    if missing_subscriber_ids:
        steps.append(_channel_step(
            "subscribe", "POST", "/users/me/subscriptions",
            subscribe_request, "planned" if dry_run else "pending",
        ))
    if update_request:
        steps.append(_channel_step(
            "configure", "PATCH", resolved_endpoint, update_request,
            "planned" if dry_run else "pending",
        ))
    if dry_run:
        if not steps:
            warnings.append("Channel already has the requested configuration")
        return MutationResult(
            MutationStatus.DRY_RUN, resolved_endpoint, dry_run=True,
            current=current, desired=desired,
            request=(
                update_request
                if update_request
                else subscribe_request if missing_subscriber_ids else {}
            ),
            readback=readback, changed_fields=sorted(
                set(changed) | ({"subscribers"} if missing_subscriber_ids else set())
            ),
            resolved_mappings=mappings, warnings=warnings, steps=steps,
            remaining_fields=sorted(
                set(changed) | ({"subscribers"} if missing_subscriber_ids else set())
            ),
        )

    if missing_subscriber_ids:
        try:
            subscribe_response = administrative_mutation_request(
                "/users/me/subscriptions", method="POST",
                request=subscribe_request,
            )
        except ZulipAPIError as exc:
            for index, step in enumerate(steps):
                if step.get("name") == "subscribe":
                    steps[index] = _channel_step(
                        "subscribe", "POST", "/users/me/subscriptions",
                        subscribe_request, "failed", error=exc.error,
                    )
                    break
            return MutationResult(
                MutationStatus.PARTIAL, resolved_endpoint, dry_run=False,
                current=current, desired=desired,
                request=subscribe_request, response=responses or None,
                readback=readback, changed_fields=sorted(
                    set(changed) | {"subscribers"}
                ),
                resolved_mappings=mappings, unsupported_fields=unsupported,
                warnings=warnings, error=exc.error, steps=steps,
                completed_fields=sorted(
                    set(desired_configuration) - set(changed)
                ),
                remaining_fields=sorted(set(changed) | {"subscribers"}),
                failed_step="subscribe",
            )
        responses["subscribe"] = subscribe_response
        for index, step in enumerate(steps):
            if step.get("name") == "subscribe":
                steps[index] = _channel_step(
                    "subscribe", "POST", "/users/me/subscriptions",
                    subscribe_request, "ok", response=subscribe_response,
                )
                break

    if update_request:
        try:
            update_response = administrative_mutation_request(
                resolved_endpoint, method="PATCH", request=update_request,
            )
        except ZulipAPIError as exc:
            for index, step in enumerate(steps):
                if step.get("name") == "configure":
                    steps[index] = _channel_step(
                        "configure", "PATCH", resolved_endpoint,
                        update_request, "failed", error=exc.error,
                    )
                    break
            return MutationResult(
                MutationStatus.PARTIAL, resolved_endpoint, dry_run=False,
                current=current, desired=desired, request=update_request,
                response=responses or None, readback=readback,
                changed_fields=changed, resolved_mappings=mappings,
                warnings=warnings, error=exc.error, steps=steps,
                completed_fields=sorted(
                    (set(desired_configuration) - set(changed))
                    | (
                        {"subscribers"}
                        if subscribers and not missing_subscriber_ids else set()
                    )
                ),
                remaining_fields=sorted(
                    set(changed)
                    | ({"subscribers"} if missing_subscriber_ids else set())
                ),
                failed_step="configure",
            )
        responses["configure"] = update_response
        for index, step in enumerate(steps):
            if step.get("name") == "configure":
                steps[index] = _channel_step(
                    "configure", "PATCH", resolved_endpoint, update_request,
                    "ok", response=update_response,
                )
                break
        ignored = update_response.get("ignored_parameters_unsupported")
        if isinstance(ignored, list):
            unsupported.extend(
                value for value in ignored if isinstance(value, str)
            )
        try:
            readback = next(
                (
                    item for item in _channel_inventory()
                    if item.get("stream_id") == stream_id
                ),
                None,
            )
        except (ZulipAPIError, ValueError) as exc:
            error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
                message=str(exc), code="READBACK_ERROR",
            )
            return MutationResult(
                MutationStatus.PARTIAL, resolved_endpoint, dry_run=False,
                current=current, desired=desired, request=update_request,
                response=responses, readback=None, changed_fields=changed,
                resolved_mappings=mappings, unsupported_fields=unsupported,
                warnings=warnings, error=error, steps=steps,
                completed_fields=sorted(
                    (set(desired_configuration) - set(changed))
                    | (
                        {"subscribers"}
                        if subscribers and not missing_subscriber_ids else set()
                    )
                ),
                remaining_fields=sorted(
                    set(changed) | ({"subscribers"} if missing_subscriber_ids else set())
                ),
                failed_step="configuration_readback",
            )
        if readback is None:
            return MutationResult(
                MutationStatus.PARTIAL, resolved_endpoint, dry_run=False,
                current=current, desired=desired, request=update_request,
                response=responses, readback=None, changed_fields=changed,
                resolved_mappings=mappings, unsupported_fields=unsupported,
                warnings=warnings,
                error=APIError(
                    message="Updated channel was absent from authoritative readback",
                    code="READBACK_ERROR",
                ),
                steps=steps,
                completed_fields=sorted(
                    set(desired_configuration) - set(changed)
                ),
                remaining_fields=sorted(
                    set(changed) | ({"subscribers"} if subscribers else set())
                ),
                failed_step="configuration_readback",
            )

    assert readback is not None
    _, verified_config_remaining, verified_config_absent, _ = (
        _channel_configuration_delta(readback, desired_configuration)
    )
    unverified_configuration = set(verified_config_remaining) | set(
        verified_config_absent
    )
    verified_configuration = set(desired_configuration) - unverified_configuration
    try:
        final_readback = readback
        final_subscribers = (
            _channel_subscriber_ids(stream_id)
            if missing_subscriber_ids
            else current_subscribers
        )
    except (ValueError, ZulipAPIError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            MutationStatus.PARTIAL, resolved_endpoint, dry_run=False,
            current=current, desired=desired, request=update_request or request,
            response=responses, readback=readback,
            changed_fields=sorted(
                set(changed) | ({"subscribers"} if missing_subscriber_ids else set())
            ),
            resolved_mappings=mappings, unsupported_fields=unsupported,
            warnings=warnings, error=error, steps=steps,
            completed_fields=sorted(verified_configuration),
            remaining_fields=sorted(
                unverified_configuration
                | ({"subscribers"} if subscribers else set())
            ),
            failed_step="final_readback",
        )
    if final_readback is None:
        final_changed = sorted(desired_configuration)
    else:
        _, final_changed, final_absent, _ = _channel_configuration_delta(
            final_readback, desired_configuration,
        )
        final_changed = sorted(set(final_changed) | set(final_absent))
    final_missing_subscribers = sorted(
        set(subscriber_ids) - final_subscribers
    )
    remaining = sorted(
        set(final_changed)
        | ({"subscribers"} if final_missing_subscribers else set())
        | set(unsupported)
    )
    completed = sorted(
        (set(desired_configuration) | ({"subscribers"} if subscribers else set()))
        - set(remaining)
    )
    if remaining:
        warnings.append(
            "Channel readback did not match requested values: "
            + ", ".join(remaining)
        )
    return MutationResult(
        MutationStatus.OK if not remaining else MutationStatus.PARTIAL,
        resolved_endpoint, dry_run=False, current=current, desired=desired,
        request=update_request or request, response=responses,
        readback=final_readback, changed_fields=sorted(
            set(changed) | ({"subscribers"} if missing_subscriber_ids else set())
        ),
        resolved_mappings=mappings, unsupported_fields=unsupported,
        warnings=warnings, steps=steps, completed_fields=completed,
        remaining_fields=remaining,
    )


def update_channel_configuration(
    realm_url: str,
    channel: str,
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/streams/{stream_id}"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    expected = expected or {}
    invalid = (set(changes) | set(expected)) - CHANNEL_UPDATE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(field for field, value in changes.items() if value is None)
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    privacy = changes.get("privacy")
    if privacy is not None and privacy not in {"public", "private", "web_public"}:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(message="privacy must be public, private, or web_public", code="INVALID_PRIVACY"),
        )
    try:
        streams = _channel_inventory()
        current_channel = _find_channel(streams, channel)
        if current_channel is None:
            raise ValueError(f"Unknown channel name: {channel}")
        stream_id = current_channel.get("stream_id")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            raise ValueError("Target channel did not have a valid stream_id")
        group_ids, user_ids = _semantic_name_maps()
        resolved_changes, change_mappings = _resolve_channel_inputs(
            changes, group_ids, user_ids,
        )
        resolved_expected, expected_mappings = _resolve_channel_inputs(
            expected, group_ids, user_ids,
        )
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    desired_privacy = resolved_changes.get("privacy", _channel_privacy(current_channel))
    desired_default = resolved_changes.get(
        "is_default_stream", current_channel.get("is_default"),
    )
    if desired_default is True and desired_privacy == "private":
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=f"/streams/{stream_id}",
            dry_run=dry_run,
            current={
                "privacy": _channel_privacy(current_channel),
                "is_default_stream": current_channel.get("is_default"),
            },
            desired=changes,
            error=APIError(
                message="A private channel cannot be a default channel",
                code="INVALID_DEFAULT_CHANNEL",
            ),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "channel": {"semantic": channel, "resolved": stream_id},
    }
    for field in sorted(set(change_mappings) | set(expected_mappings)):
        mapping: dict[str, JSONValue] = {}
        if field in change_mappings:
            mapping["desired"] = change_mappings[field]
        if field in expected_mappings:
            mapping["expected"] = expected_mappings[field]
        mappings[field] = mapping

    current: dict[str, JSONValue] = {}
    absent = []
    for field in set(changes) | set(expected):
        present, value = _channel_current(current_channel, field)
        if present:
            current[field] = value
        else:
            absent.append(field)
    if absent:
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            current=current,
            desired=changes,
            unsupported_fields=sorted(absent),
            resolved_mappings=mappings,
            error=APIError(
                message=f"Channel response omitted fields: {', '.join(sorted(absent))}",
                code="UNSUPPORTED_FIELD",
            ),
        )
    mismatches = sorted(
        field for field, value in resolved_expected.items()
        if _channel_value(field, current[field]) != value
    )
    if mismatches:
        result = _conflict_result(
            endpoint, dry_run, current, changes, mismatches, [],
        )
        result.resolved_mappings = mappings
        return result
    changed = sorted(
        field for field, value in resolved_changes.items()
        if _channel_value(field, current[field]) != value
    )
    request: dict[str, JSONValue] = {}
    for field in changed:
        value = resolved_changes[field]
        if field in CHANNEL_GROUP_FIELDS:
            request[field] = {"new": value, "old": current[field]}
        elif field == "privacy":
            request["is_private"] = value == "private"
            request["is_web_public"] = value == "web_public"
        else:
            request[field] = value
    resolved_endpoint = f"/streams/{stream_id}"
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request,
            changed_fields=changed,
            resolved_mappings=mappings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            readback=current,
            resolved_mappings=mappings,
            warnings=["Channel already had the desired configuration"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback_channel = next(
            (
                item for item in _channel_inventory()
                if item.get("stream_id") == stream_id
            ),
            None,
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            error=error,
        )
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = [value for value in ignored if isinstance(value, str)] if isinstance(ignored, list) else []
    readback: dict[str, JSONValue] = {}
    mismatched_readback = []
    if readback_channel is None:
        mismatched_readback = list(changes)
    else:
        for field, value in resolved_changes.items():
            present, actual = _channel_current(readback_channel, field)
            if present:
                readback[field] = actual
            if not present or _channel_value(field, actual) != value:
                mismatched_readback.append(field)
    warnings = []
    if mismatched_readback:
        warnings.append(
            f"Readback did not match requested values: {', '.join(mismatched_readback)}"
        )
    return MutationResult(
        status=(
            MutationStatus.PARTIAL
            if unsupported or mismatched_readback
            else MutationStatus.OK
        ),
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=warnings,
    )


def subscribe_users_to_channel(
    realm_url: str,
    channel: str,
    users: list[str],
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/users/me/subscriptions"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    try:
        streams = _channel_inventory()
        current_channel = _find_channel(streams, channel)
        if current_channel is None:
            raise ValueError(f"Unknown channel name: {channel}")
        stream_id = current_channel.get("stream_id")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            raise ValueError("Target channel did not have a valid stream_id")
        _, user_ids = _semantic_name_maps()
        resolved_users = [_named_id(user, user_ids, "user") for user in users]
        subscriber_response = get_channel_subscribers_configuration(stream_id)
        subscribers = subscriber_response.get("subscribers")
        if not isinstance(subscribers, list):
            raise ValueError("Channel subscriber inventory was absent or null")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"channel": channel, "users": users},
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    current_ids = {
        user_id for user_id in subscribers
        if isinstance(user_id, int) and not isinstance(user_id, bool)
    }
    missing_ids = [user_id for user_id in resolved_users if user_id not in current_ids]
    missing_names = [
        user for user, user_id in zip(users, resolved_users) if user_id in missing_ids
    ]
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "channel": {"semantic": channel, "resolved": stream_id},
        "users": [
            {"semantic": user, "resolved": user_id}
            for user, user_id in zip(users, resolved_users)
        ],
    }
    request: dict[str, JSONValue] = {
        "subscriptions": [{"name": current_channel.get("name")}],
        "principals": missing_ids,
        "authorization_errors_fatal": True,
    }
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "users": users},
            request=request if missing_ids else {},
            changed_fields=missing_names,
            resolved_mappings=mappings,
        )
    if not missing_ids:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=endpoint,
            dry_run=False,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "users": users},
            readback={"subscriber_ids": sorted(current_ids)},
            resolved_mappings=mappings,
            warnings=["All selected users were already subscribed"],
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="POST", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.desired = {"channel": channel, "users": users}
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback_response = get_channel_subscribers_configuration(stream_id)
    except ZulipAPIError as exc:
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "users": users},
            request=request,
            response=response,
            changed_fields=missing_names,
            resolved_mappings=mappings,
            error=exc.error,
        )
    readback_subscribers = readback_response.get("subscribers")
    readback_ids = (
        {
            user_id for user_id in readback_subscribers
            if isinstance(user_id, int) and not isinstance(user_id, bool)
        }
        if isinstance(readback_subscribers, list)
        else set()
    )
    missing_readback = [user_id for user_id in missing_ids if user_id not in readback_ids]
    unauthorized = response.get("unauthorized")
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = [value for value in ignored if isinstance(value, str)] if isinstance(ignored, list) else []
    warnings = []
    if missing_readback:
        warnings.append("Some selected users were absent from subscription readback")
    if isinstance(unauthorized, list) and unauthorized:
        warnings.append("Zulip reported unauthorized channel subscriptions")
    return MutationResult(
        status=(
            MutationStatus.PARTIAL
            if missing_readback or unsupported or (isinstance(unauthorized, list) and unauthorized)
            else MutationStatus.OK
        ),
        endpoint=endpoint,
        dry_run=False,
        current={"subscriber_ids": sorted(current_ids)},
        desired={"channel": channel, "users": users},
        request=request,
        response=response,
        readback={"subscriber_ids": sorted(readback_ids)},
        changed_fields=missing_names,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=warnings,
    )


def _channel_membership_state(
    realm_url: str,
    channel: str,
    users: list[str],
    dry_run: bool,
) -> tuple[
    dict[str, JSONValue] | None,
    dict[str, JSONValue] | None,
    int | None,
    list[int],
    set[int],
    dict[str, JSONValue],
    MutationResult | None,
]:
    endpoint = "/users/me/subscriptions"
    server, principal, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return None, None, None, [], set(), {}, failure
    assert server is not None and principal is not None
    if not isinstance(channel, str) or channel.isdigit():
        return None, None, None, [], set(), {}, _semantic_error(
            endpoint, dry_run, "Numeric channel IDs are not accepted",
        )
    try:
        streams = _channel_inventory()
        current_channel = _find_channel(streams, channel)
        if current_channel is None:
            raise ValueError(f"Unknown channel name: {channel}")
        stream_id = current_channel.get("stream_id")
        if not isinstance(stream_id, int) or isinstance(stream_id, bool):
            raise ValueError("Target channel did not have a valid stream_id")
        _, user_ids = _semantic_name_maps()
        resolved_users = [_named_id(user, user_ids, "user") for user in users]
        subscriber_response = get_channel_subscribers_configuration(stream_id)
        subscribers = subscriber_response.get("subscribers")
        if not isinstance(subscribers, list):
            raise ValueError("Channel subscriber inventory was absent or null")
    except ZulipAPIError as exc:
        return None, None, None, [], set(), {}, _mutation_failure(
            endpoint, dry_run, exc,
        )
    except ValueError as exc:
        return None, None, None, [], set(), {}, _semantic_error(
            endpoint, dry_run, str(exc),
        )
    current_ids = {
        user_id for user_id in subscribers
        if isinstance(user_id, int) and not isinstance(user_id, bool)
    }
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "channel": {"semantic": channel, "resolved": stream_id},
        "users": [
            {"semantic": user, "resolved": user_id}
            for user, user_id in zip(users, resolved_users)
        ],
    }
    return (
        current_channel, principal, stream_id, resolved_users,
        current_ids, mappings, None,
    )


def unsubscribe_users_from_channel(
    realm_url: str,
    channel: str,
    users: list[str],
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/users/me/subscriptions"
    current_channel, principal, stream_id, resolved, current_ids, mappings, failure = (
        _channel_membership_state(realm_url, channel, users, dry_run)
    )
    if failure is not None:
        return failure
    assert current_channel is not None and principal is not None and stream_id is not None
    remove_ids = [user_id for user_id in resolved if user_id in current_ids]
    remove_names = [
        user for user, user_id in zip(users, resolved) if user_id in remove_ids
    ]
    principal_id = principal.get("user_id")
    if (
        current_channel.get("invite_only") is True
        and isinstance(principal_id, int)
        and principal_id in remove_ids
    ):
        return MutationResult(
            MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "remove_users": users},
            resolved_mappings=mappings,
            error=APIError(
                message="Cannot remove the authenticated administrator from a private channel",
                code="PRIVATE_CHANNEL_ADMIN_PROTECTED",
            ),
        )
    request: dict[str, JSONValue] = {
        "subscriptions": [current_channel.get("name")], "principals": remove_ids,
    }
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, endpoint, dry_run=True,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "remove_users": users},
            request=request if remove_ids else {}, changed_fields=remove_names,
            resolved_mappings=mappings,
        )
    if not remove_ids:
        return MutationResult(
            MutationStatus.OK, endpoint, dry_run=False,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "remove_users": users},
            readback={"subscriber_ids": sorted(current_ids)},
            resolved_mappings=mappings,
            warnings=["None of the selected users were subscribed"],
        )
    try:
        response = administrative_mutation_request(endpoint, "DELETE", request)
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, False, exc)
        result.current = {"subscriber_ids": sorted(current_ids)}
        result.desired = {"channel": channel, "remove_users": users}
        result.request, result.resolved_mappings = request, mappings
        return result
    try:
        readback = get_channel_subscribers_configuration(stream_id).get("subscribers")
        if not isinstance(readback, list):
            raise ValueError("Channel subscriber readback was absent or null")
        readback_ids = {
            user_id for user_id in readback
            if isinstance(user_id, int) and not isinstance(user_id, bool)
        }
    except (ValueError, ZulipAPIError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            MutationStatus.PARTIAL, endpoint, dry_run=False,
            current={"subscriber_ids": sorted(current_ids)},
            desired={"channel": channel, "remove_users": users}, request=request,
            response=response, changed_fields=remove_names,
            resolved_mappings=mappings, error=error,
        )
    remaining = sorted(set(remove_ids) & readback_ids)
    return MutationResult(
        MutationStatus.PARTIAL if remaining else MutationStatus.OK,
        endpoint, dry_run=False,
        current={"subscriber_ids": sorted(current_ids)},
        desired={"channel": channel, "remove_users": users}, request=request,
        response=response, readback={"subscriber_ids": sorted(readback_ids)},
        changed_fields=remove_names, resolved_mappings=mappings,
        warnings=["Some selected users remained subscribed after readback"] if remaining else [],
    )


def set_channel_members(
    realm_url: str,
    channel: str,
    users: list[str],
    expected_users: list[str] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/users/me/subscriptions"
    requested = list(dict.fromkeys(users + list(expected_users or [])))
    current_channel, principal, stream_id, resolved_all, current_ids, mappings, failure = (
        _channel_membership_state(realm_url, channel, requested, dry_run)
    )
    if failure is not None:
        return failure
    assert current_channel is not None and principal is not None and stream_id is not None
    resolved_by_name = dict(zip(requested, resolved_all))
    desired_ids = {resolved_by_name[user] for user in users}
    expected_ids = (
        {resolved_by_name[user] for user in expected_users}
        if expected_users is not None else None
    )
    mappings["users"] = [
        {"semantic": user, "resolved": resolved_by_name[user]} for user in users
    ]
    mappings["expected_users"] = [
        {"semantic": user, "resolved": resolved_by_name[user]}
        for user in (expected_users or [])
    ]
    current = {"subscriber_ids": sorted(current_ids)}
    desired = {"channel": channel, "users": users}
    if expected_ids is not None and expected_ids != current_ids:
        return MutationResult(
            MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
            current=current, desired=desired, resolved_mappings=mappings,
            error=APIError(
                message="Current channel members did not match expected_users",
                code="EXPECTED_VALUE_MISMATCH",
            ),
        )
    add_ids = sorted(desired_ids - current_ids)
    remove_ids = sorted(current_ids - desired_ids)
    principal_id = principal.get("user_id")
    if (
        current_channel.get("invite_only") is True
        and isinstance(principal_id, int)
        and principal_id in current_ids
        and principal_id in remove_ids
    ):
        return MutationResult(
            MutationStatus.CONFLICT, endpoint, dry_run=dry_run,
            current=current, desired=desired, resolved_mappings=mappings,
            error=APIError(
                message="Cannot remove the authenticated administrator from a private channel",
                code="PRIVATE_CHANNEL_ADMIN_PROTECTED",
            ),
        )
    summary: dict[str, JSONValue] = {
        "to_subscribe": add_ids, "to_unsubscribe": remove_ids,
    }
    if not add_ids and not remove_ids:
        return MutationResult(
            MutationStatus.OK, endpoint, dry_run=dry_run,
            current=current, desired=desired, request=summary,
            readback=current, resolved_mappings=mappings,
            warnings=["Channel already had the exact requested membership"],
        )
    if dry_run:
        return MutationResult(
            MutationStatus.DRY_RUN, endpoint, dry_run=True,
            current=current, desired=desired, request=summary,
            changed_fields=["members"], resolved_mappings=mappings,
        )
    completed_additions: list[int] = []
    completed_removals: list[int] = []
    try:
        if add_ids:
            administrative_mutation_request(endpoint, "POST", {
                "subscriptions": [{"name": current_channel.get("name")}],
                "principals": add_ids,
                "authorization_errors_fatal": True,
            })
            completed_additions = add_ids
        if remove_ids:
            administrative_mutation_request(endpoint, "DELETE", {
                "subscriptions": [current_channel.get("name")],
                "principals": remove_ids,
            })
            completed_removals = remove_ids
    except ZulipAPIError as exc:
        changed_any = bool(completed_additions or completed_removals)
        return MutationResult(
            MutationStatus.PARTIAL if changed_any else _mutation_status(exc),
            endpoint, dry_run=False, current=current, desired=desired,
            request=summary,
            response={
                "subscribed": completed_additions,
                "unsubscribed": completed_removals,
                "remaining_subscriptions": [
                    user_id for user_id in add_ids if user_id not in completed_additions
                ],
                "remaining_unsubscriptions": [
                    user_id for user_id in remove_ids if user_id not in completed_removals
                ],
            },
            changed_fields=["members"], resolved_mappings=mappings,
            error=exc.error,
        )
    try:
        readback = get_channel_subscribers_configuration(stream_id).get("subscribers")
        if not isinstance(readback, list):
            raise ValueError("Channel subscriber readback was absent or null")
        readback_ids = {
            user_id for user_id in readback
            if isinstance(user_id, int) and not isinstance(user_id, bool)
        }
    except (ValueError, ZulipAPIError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            MutationStatus.PARTIAL, endpoint, dry_run=False,
            current=current, desired=desired, request=summary,
            response={"subscribed": add_ids, "unsubscribed": remove_ids},
            changed_fields=["members"], resolved_mappings=mappings,
            error=error,
        )
    confirmed = readback_ids == desired_ids
    return MutationResult(
        MutationStatus.OK if confirmed else MutationStatus.PARTIAL,
        endpoint, dry_run=False, current=current, desired=desired, request=summary,
        response={"subscribed": add_ids, "unsubscribed": remove_ids},
        readback={"subscriber_ids": sorted(readback_ids)},
        changed_fields=["members"], resolved_mappings=mappings,
        warnings=[] if confirmed else ["Membership readback did not match exact desired state"],
    )


def _group_inventory() -> list[dict[str, JSONValue]]:
    response = get_user_groups_configuration()
    groups = response.get("user_groups")
    if not isinstance(groups, list):
        raise ValueError("User-group inventory was absent or null")
    return [group for group in groups if isinstance(group, dict)]


def _find_group(
    groups: list[dict[str, JSONValue]], name: str,
) -> dict[str, JSONValue] | None:
    matches = [group for group in groups if group.get("name") == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous user-group name: {name}")
    return None


def _group_id(group: dict[str, JSONValue]) -> int:
    group_id = group.get("id")
    if not isinstance(group_id, int) or isinstance(group_id, bool):
        raise ValueError("Target user group did not have a valid id")
    return group_id


def _group_list_value(group: dict[str, JSONValue], field: str) -> list[int]:
    value = group.get(field)
    if not isinstance(value, list):
        raise ValueError(f"User-group response omitted {field}")
    return sorted(
        item for item in value
        if isinstance(item, int) and not isinstance(item, bool)
    )


def _resolve_group_fields(
    values: dict[str, JSONValue],
    group_ids: dict[str, int],
    user_ids: dict[str, int],
) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    resolved = dict(values)
    mappings: dict[str, JSONValue] = {}
    for field in sorted(set(values) & USER_GROUP_PERMISSION_FIELDS):
        value = _resolve_group_setting(values[field], group_ids, user_ids)
        resolved[field] = value
        mappings[field] = {"semantic": values[field], "resolved": value}
    return resolved, mappings


def _canonical_group_setting(value: JSONValue) -> JSONValue:
    if not isinstance(value, dict):
        return value
    members = value.get("direct_members")
    subgroups = value.get("direct_subgroups")
    if not isinstance(members, list) or not isinstance(subgroups, list):
        return value
    return {
        "direct_members": sorted(members, key=repr),
        "direct_subgroups": sorted(subgroups, key=repr),
    }


def create_user_group(
    realm_url: str,
    name: str,
    description: str,
    members: list[str],
    subgroups: list[str] | None = None,
    permissions: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/user_groups/create"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    subgroups = list(subgroups or [])
    permissions = dict(permissions or {})
    invalid = set(permissions) - USER_GROUP_PERMISSION_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    null_fields = sorted(field for field, value in permissions.items() if value is None)
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"name": name},
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    try:
        groups = _group_inventory()
        group_ids, user_ids = _semantic_name_maps()
        member_ids = sorted({_named_id(member, user_ids, "user") for member in members})
        subgroup_ids = sorted({_named_id(group, group_ids, "group") for group in subgroups})
        resolved_permissions, permission_mappings = _resolve_group_fields(
            permissions, group_ids, user_ids,
        )
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={"name": name},
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    desired: dict[str, JSONValue] = {
        "name": name,
        "description": description,
        "members": members,
        "subgroups": subgroups,
        "permissions": permissions,
    }
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "members": [
            {"semantic": member, "resolved": _named_id(member, user_ids, "user")}
            for member in members
        ],
        "subgroups": [
            {"semantic": group, "resolved": _named_id(group, group_ids, "group")}
            for group in subgroups
        ],
        **permission_mappings,
    }
    existing = _find_group(groups, name)
    if existing is not None:
        mismatches = []
        if existing.get("description") != description:
            mismatches.append("description")
        try:
            if _group_list_value(existing, "members") != member_ids:
                mismatches.append("members")
            if _group_list_value(existing, "direct_subgroup_ids") != subgroup_ids:
                mismatches.append("subgroups")
        except ValueError as exc:
            return MutationResult(
                status=MutationStatus.UNSUPPORTED,
                endpoint=endpoint,
                dry_run=dry_run,
                current=existing,
                desired=desired,
                resolved_mappings=mappings,
                error=APIError(message=str(exc), code="UNSUPPORTED_FIELD"),
            )
        for field, value in resolved_permissions.items():
            if (
                field not in existing
                or _canonical_group_setting(existing[field])
                != _canonical_group_setting(value)
            ):
                mismatches.append(field)
        if mismatches:
            return MutationResult(
                status=MutationStatus.CONFLICT,
                endpoint=endpoint,
                dry_run=dry_run,
                current=existing,
                desired=desired,
                resolved_mappings=mappings,
                error=APIError(
                    message=f"User group already exists with different settings: {', '.join(mismatches)}",
                    code="USER_GROUP_ALREADY_EXISTS",
                ),
            )
        return MutationResult(
            status=MutationStatus.DRY_RUN if dry_run else MutationStatus.OK,
            endpoint=endpoint,
            dry_run=dry_run,
            current=existing,
            desired=desired,
            readback=existing,
            resolved_mappings=mappings,
            warnings=["User group already exists with the requested configuration"],
        )
    request: dict[str, JSONValue] = {
        "name": name,
        "description": description,
        "members": member_ids,
        "subgroups": subgroup_ids,
        **resolved_permissions,
    }
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            desired=desired,
            request=request,
            changed_fields=sorted(request),
            resolved_mappings=mappings,
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="POST", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.desired = desired
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback = _find_group(_group_inventory(), name)
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            desired=desired,
            request=request,
            response=response,
            resolved_mappings=mappings,
            error=error,
        )
    mismatches = []
    if readback is None:
        mismatches.append("name")
    else:
        if readback.get("description") != description:
            mismatches.append("description")
        try:
            if _group_list_value(readback, "members") != member_ids:
                mismatches.append("members")
            if _group_list_value(readback, "direct_subgroup_ids") != subgroup_ids:
                mismatches.append("subgroups")
        except ValueError as exc:
            mismatches.append(str(exc))
        for field, value in resolved_permissions.items():
            if (
                field not in readback
                or _canonical_group_setting(readback[field])
                != _canonical_group_setting(value)
            ):
                mismatches.append(field)
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = (
        [value for value in ignored if isinstance(value, str)]
        if isinstance(ignored, list) else []
    )
    return MutationResult(
        status=MutationStatus.PARTIAL if mismatches or unsupported else MutationStatus.OK,
        endpoint=endpoint,
        dry_run=False,
        desired=desired,
        request=request,
        response=response,
        readback=readback,
        changed_fields=sorted(request),
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=(
            ["User-group readback did not match: " + ", ".join(mismatches)]
            if mismatches else []
        ),
    )


def update_user_group(
    realm_url: str,
    group: str,
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/user_groups/{user_group_id}"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    expected = dict(expected or {})
    invalid = (set(changes) | set(expected)) - USER_GROUP_UPDATE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(
        field for field, value in {**expected, **changes}.items() if value is None
    )
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Null is not a supported value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    try:
        groups = _group_inventory()
        current_group = _find_group(groups, group)
        if current_group is None:
            raise ValueError(f"Unknown user-group name: {group}")
        group_id = _group_id(current_group)
        group_ids, user_ids = _semantic_name_maps()
        resolved_changes, change_mappings = _resolve_group_fields(
            changes, group_ids, user_ids,
        )
        resolved_expected, expected_mappings = _resolve_group_fields(
            expected, group_ids, user_ids,
        )
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "group": {"semantic": group, "resolved": group_id},
    }
    for field in sorted(set(change_mappings) | set(expected_mappings)):
        mapping: dict[str, JSONValue] = {}
        if field in change_mappings:
            mapping["desired"] = change_mappings[field]
        if field in expected_mappings:
            mapping["expected"] = expected_mappings[field]
        mappings[field] = mapping
    missing = sorted(
        field for field in set(changes) | set(expected) if field not in current_group
    )
    if missing:
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            current=current_group,
            desired=changes,
            unsupported_fields=missing,
            resolved_mappings=mappings,
            error=APIError(
                message=f"User-group response omitted fields: {', '.join(missing)}",
                code="UNSUPPORTED_FIELD",
            ),
        )
    mismatches = sorted(
        field for field, value in resolved_expected.items()
        if _canonical_group_setting(current_group[field])
        != _canonical_group_setting(value)
    )
    current = {field: current_group[field] for field in set(changes) | set(expected)}
    if mismatches:
        result = _conflict_result(endpoint, dry_run, current, changes, mismatches, [])
        result.resolved_mappings = mappings
        return result
    changed = sorted(
        field for field, value in resolved_changes.items()
        if _canonical_group_setting(current_group[field])
        != _canonical_group_setting(value)
    )
    request: dict[str, JSONValue] = {}
    for field in changed:
        value = resolved_changes[field]
        request[field] = (
            {"old": current_group[field], "new": value}
            if field in USER_GROUP_PERMISSION_FIELDS else value
        )
    resolved_endpoint = f"/user_groups/{group_id}"
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request,
            changed_fields=changed,
            resolved_mappings=mappings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            readback=current,
            resolved_mappings=mappings,
            warnings=["User group already had the desired configuration"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback_group = next(
            (item for item in _group_inventory() if item.get("id") == group_id), None,
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            error=error,
        )
    readback: dict[str, JSONValue] = {}
    failed = []
    for field, value in resolved_changes.items():
        if readback_group is not None and field in readback_group:
            readback[field] = readback_group[field]
        if (
            readback_group is None
            or field not in readback_group
            or _canonical_group_setting(readback_group[field])
            != _canonical_group_setting(value)
        ):
            failed.append(field)
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = (
        [value for value in ignored if isinstance(value, str)]
        if isinstance(ignored, list) else []
    )
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=(
            ["User-group readback did not match: " + ", ".join(failed)]
            if failed else []
        ),
    )


def set_user_group_members(
    realm_url: str,
    group: str,
    members: list[str],
    subgroups: list[str] | None = None,
    expected_members: list[str] | None = None,
    expected_subgroups: list[str] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/user_groups/{user_group_id}/members"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    requested_subgroups = None if subgroups is None else list(subgroups)
    try:
        groups = _group_inventory()
        current_group = _find_group(groups, group)
        if current_group is None:
            raise ValueError(f"Unknown user-group name: {group}")
        group_id = _group_id(current_group)
        group_ids, user_ids = _semantic_name_maps()
        desired_member_ids = sorted({_named_id(name, user_ids, "user") for name in members})
        requested_subgroup_ids = (
            sorted({_named_id(name, group_ids, "group") for name in requested_subgroups})
            if requested_subgroups is not None else None
        )
        if requested_subgroup_ids is not None and group_id in requested_subgroup_ids:
            raise ValueError("A user group cannot be its own subgroup")
        expected_member_ids = (
            sorted({_named_id(name, user_ids, "user") for name in expected_members})
            if expected_members is not None else None
        )
        expected_subgroup_ids = (
            sorted({_named_id(name, group_ids, "group") for name in expected_subgroups})
            if expected_subgroups is not None else None
        )
        member_response = configuration_request(
            f"/user_groups/{group_id}/members",
            request={"direct_member_only": True},
        )
        current_members_raw = member_response.get("members")
        if not isinstance(current_members_raw, list):
            raise ValueError("Direct user-group members were absent or null")
        current_member_ids = sorted(
            item for item in current_members_raw
            if isinstance(item, int) and not isinstance(item, bool)
        )
        current_subgroup_ids = _group_list_value(current_group, "direct_subgroup_ids")
        desired_subgroup_ids = (
            current_subgroup_ids
            if requested_subgroup_ids is None else requested_subgroup_ids
        )
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired={
                "group": group,
                "members": members,
                **(
                    {"subgroups": requested_subgroups}
                    if requested_subgroups is not None else {}
                ),
            },
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    current: dict[str, JSONValue] = {
        "member_ids": current_member_ids,
        "subgroup_ids": current_subgroup_ids,
    }
    desired: dict[str, JSONValue] = {
        "group": group,
        "members": members,
        **(
            {"subgroups": requested_subgroups}
            if requested_subgroups is not None else {}
        ),
    }
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "group": {"semantic": group, "resolved": group_id},
        "members": [
            {"semantic": name, "resolved": _named_id(name, user_ids, "user")}
            for name in members
        ],
        **(
            {"subgroups": [
                {"semantic": name, "resolved": _named_id(name, group_ids, "group")}
                for name in requested_subgroups
            ]}
            if requested_subgroups is not None else {}
        ),
    }
    expected_mismatch = (
        expected_member_ids is not None and expected_member_ids != current_member_ids
    ) or (
        expected_subgroup_ids is not None and expected_subgroup_ids != current_subgroup_ids
    )
    if expected_mismatch:
        return _conflict_result(
            endpoint, dry_run, current, desired, ["membership"], [],
        )
    add = sorted(set(desired_member_ids) - set(current_member_ids))
    delete = sorted(set(current_member_ids) - set(desired_member_ids))
    add_subgroups = sorted(set(desired_subgroup_ids) - set(current_subgroup_ids))
    delete_subgroups = sorted(set(current_subgroup_ids) - set(desired_subgroup_ids))
    request: dict[str, JSONValue] = {
        key: value for key, value in {
            "add": add,
            "delete": delete,
            "add_subgroups": add_subgroups,
            "delete_subgroups": delete_subgroups,
        }.items() if value
    }
    resolved_endpoint = f"/user_groups/{group_id}/members"
    changed = [key for key in ("add", "delete", "add_subgroups", "delete_subgroups") if key in request]
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=desired,
            request=request,
            changed_fields=changed,
            resolved_mappings=mappings,
        )
    if not request:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=desired,
            readback=current,
            resolved_mappings=mappings,
            warnings=["User group already had the desired direct membership"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="POST", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = desired
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback_members = configuration_request(
            resolved_endpoint, request={"direct_member_only": True},
        ).get("members")
        readback_group = next(
            (item for item in _group_inventory() if item.get("id") == group_id), None,
        )
        if not isinstance(readback_members, list) or readback_group is None:
            raise ValueError("User-group membership readback was absent or null")
        readback_member_ids = sorted(
            item for item in readback_members
            if isinstance(item, int) and not isinstance(item, bool)
        )
        readback_subgroup_ids = _group_list_value(readback_group, "direct_subgroup_ids")
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=desired,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            error=error,
        )
    readback: dict[str, JSONValue] = {
        "member_ids": readback_member_ids,
        "subgroup_ids": readback_subgroup_ids,
    }
    verified = (
        readback_member_ids == desired_member_ids
        and readback_subgroup_ids == desired_subgroup_ids
    )
    ignored = response.get("ignored_parameters_unsupported")
    unsupported = (
        [value for value in ignored if isinstance(value, str)]
        if isinstance(ignored, list) else []
    )
    return MutationResult(
        status=(
            MutationStatus.OK
            if verified and not unsupported else MutationStatus.PARTIAL
        ),
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=desired,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=[] if verified else ["User-group membership readback did not match"],
    )


def _configuration_items(
    response: dict[str, JSONValue], field: str, label: str,
) -> list[dict[str, JSONValue]]:
    items = response.get(field)
    if not isinstance(items, list):
        raise ValueError(f"{label} inventory was absent or null")
    return [item for item in items if isinstance(item, dict)]


def _ignored_parameters(response: dict[str, JSONValue]) -> list[str]:
    ignored = response.get("ignored_parameters_unsupported")
    return (
        [value for value in ignored if isinstance(value, str)]
        if isinstance(ignored, list) else []
    )


def _find_named_object(
    items: list[dict[str, JSONValue]], field: str, value: str, label: str,
) -> dict[str, JSONValue] | None:
    matches = [item for item in items if item.get(field) == value]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous {label}: {value}")
    return None


def _profile_value(field: str, value: JSONValue) -> JSONValue:
    if field != "field_data" or not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


def _profile_current(
    profile_field: dict[str, JSONValue], field: str,
) -> tuple[bool, JSONValue]:
    if field == "field_type":
        return "type" in profile_field, profile_field.get("type")
    if field in {"display_in_profile_summary", "use_for_user_matching"}:
        return True, profile_field.get(field, False)
    return field in profile_field, _profile_value(field, profile_field.get(field))


def create_custom_profile_field(
    realm_url: str,
    name: str,
    field_type: int,
    settings: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/profile_fields"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    settings = dict(settings or {})
    invalid = set(settings) - (PROFILE_FIELD_CREATE_FIELDS - {"name"})
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not isinstance(field_type, int) or isinstance(field_type, bool):
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(message="field_type must be an integer", code="INVALID_FIELD_TYPE"),
        )
    null_fields = sorted(field for field, value in settings.items() if value is None)
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message=f"Null is not a supported write value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    feature_level = server.get("zulip_feature_level")
    if "use_for_user_matching" in settings and (
        not isinstance(feature_level, int) or feature_level < 455
    ):
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            unsupported_fields=["use_for_user_matching"],
            error=APIError(
                message="use_for_user_matching requires Zulip feature level 455",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    desired = {"name": name, "field_type": field_type, **settings}
    try:
        fields = _configuration_items(
            get_profile_fields_configuration(), "custom_fields", "Profile-field",
        )
        existing = _find_named_object(fields, "name", name, "profile-field name")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=desired,
            error=APIError(message=str(exc), code="INVENTORY_ERROR"),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
    }
    if existing is not None:
        mismatches = []
        for field, value in desired.items():
            present, current_value = _profile_current(existing, field)
            if not present or current_value != _profile_value(field, value):
                mismatches.append(field)
        if mismatches:
            return MutationResult(
                status=MutationStatus.CONFLICT,
                endpoint=endpoint,
                dry_run=dry_run,
                current=existing,
                desired=desired,
                resolved_mappings=mappings,
                error=APIError(
                    message=f"Profile field already exists with different settings: {', '.join(mismatches)}",
                    code="PROFILE_FIELD_ALREADY_EXISTS",
                ),
            )
        return MutationResult(
            status=MutationStatus.DRY_RUN if dry_run else MutationStatus.OK,
            endpoint=endpoint,
            dry_run=dry_run,
            current=existing,
            desired=desired,
            readback=existing,
            resolved_mappings=mappings,
            warnings=["Profile field already exists with the requested configuration"],
        )
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            desired=desired,
            request=desired,
            changed_fields=sorted(desired),
            resolved_mappings=mappings,
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="POST", request=desired,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.desired = desired
        result.request = desired
        result.resolved_mappings = mappings
        return result
    try:
        readback_fields = _configuration_items(
            get_profile_fields_configuration(), "custom_fields", "Profile-field",
        )
        field_id = response.get("id")
        readback = next(
            (
                item for item in readback_fields
                if isinstance(field_id, int) and item.get("id") == field_id
            ),
            None,
        )
        if readback is None:
            readback = _find_named_object(
                readback_fields, "name", name, "profile-field name",
            )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            desired=desired,
            request=desired,
            response=response,
            resolved_mappings=mappings,
            error=error,
        )
    failed = []
    for field, value in desired.items():
        present, current_value = (
            _profile_current(readback, field)
            if readback is not None else (False, None)
        )
        if not present or current_value != _profile_value(field, value):
            failed.append(field)
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=endpoint,
        dry_run=False,
        desired=desired,
        request=desired,
        response=response,
        readback=readback,
        changed_fields=sorted(desired),
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=(
            ["Profile-field readback did not match: " + ", ".join(failed)]
            if failed else []
        ),
    )


def update_custom_profile_field(
    realm_url: str,
    field: str,
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/profile_fields/{field_id}"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    expected = dict(expected or {})
    invalid = (set(changes) | set(expected)) - PROFILE_FIELD_UPDATE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(
        key for key, value in {**expected, **changes}.items() if value is None
    )
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message=f"Null is not a supported value for: {', '.join(null_fields)}",
                code="NULL_WRITE_VALUE",
            ),
        )
    feature_level = server.get("zulip_feature_level")
    if "use_for_user_matching" in set(changes) | set(expected) and (
        not isinstance(feature_level, int) or feature_level < 455
    ):
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            unsupported_fields=["use_for_user_matching"],
            error=APIError(
                message="use_for_user_matching requires Zulip feature level 455",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    try:
        fields = _configuration_items(
            get_profile_fields_configuration(), "custom_fields", "Profile-field",
        )
        current_field = _find_named_object(fields, "name", field, "profile-field name")
        if current_field is None:
            raise ValueError(f"Unknown profile-field name: {field}")
        field_id = current_field.get("id")
        if not isinstance(field_id, int) or isinstance(field_id, bool):
            raise ValueError("Target profile field did not have a valid id")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    current_values: dict[str, JSONValue] = {}
    missing = []
    for key in set(changes) | set(expected):
        present, value = _profile_current(current_field, key)
        if present:
            current_values[key] = value
        else:
            missing.append(key)
    missing.sort()
    if missing:
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            current=current_field,
            desired=changes,
            unsupported_fields=missing,
            error=APIError(message="Profile-field response omitted requested fields", code="UNSUPPORTED_FIELD"),
        )
    mismatches = sorted(
        key for key, value in expected.items()
        if current_values[key] != _profile_value(key, value)
    )
    current = current_values
    if mismatches:
        return _conflict_result(endpoint, dry_run, current, changes, mismatches, [])
    changed = sorted(
        key for key, value in changes.items()
        if current_values[key] != _profile_value(key, value)
    )
    request = {key: changes[key] for key in changed}
    resolved_endpoint = f"/realm/profile_fields/{field_id}"
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "field": {"semantic": field, "resolved": field_id},
    }
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request,
            changed_fields=changed,
            resolved_mappings=mappings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            readback=current,
            resolved_mappings=mappings,
            warnings=["Profile field already had the desired configuration"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback = next(
            (
                item for item in _configuration_items(
                    get_profile_fields_configuration(), "custom_fields", "Profile-field",
                ) if item.get("id") == field_id
            ),
            None,
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            error=error,
        )
    failed = []
    for key, value in changes.items():
        present, actual = (
            _profile_current(readback, key)
            if readback is not None else (False, None)
        )
        if not present or actual != _profile_value(key, value):
            failed.append(key)
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=readback,
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=(
            ["Profile-field readback did not match: " + ", ".join(failed)]
            if failed else []
        ),
    )


def _owner_destination(
    endpoint: str, realm_url: str, dry_run: bool,
) -> tuple[dict[str, JSONValue] | None, MutationResult | None]:
    server, principal, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return None, failure
    assert server is not None and principal is not None
    if principal.get("is_owner") is not True:
        return None, MutationResult(
            status=MutationStatus.FORBIDDEN,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message="Allowed-domain writes require an organization owner",
                code="OWNER_REQUIRED",
            ),
        )
    return server, None


def add_allowed_domain(
    realm_url: str,
    domain: str,
    allow_subdomains: bool,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/domains"
    server, failure = _owner_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    normalized = domain.strip().lower()
    if not normalized:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(message="domain must not be empty", code="INVALID_DOMAIN"),
        )
    desired = {"domain": normalized, "allow_subdomains": allow_subdomains}
    try:
        domains = _configuration_items(
            get_domains_configuration(), "domains", "Allowed-domain",
        )
        existing = _find_named_object(domains, "domain", normalized, "allowed domain")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=desired,
            error=APIError(message=str(exc), code="INVENTORY_ERROR"),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "domain": {"semantic": domain, "resolved": normalized},
    }
    if existing is not None:
        if existing.get("allow_subdomains") != allow_subdomains:
            return MutationResult(
                status=MutationStatus.CONFLICT,
                endpoint=endpoint,
                dry_run=dry_run,
                current=existing,
                desired=desired,
                resolved_mappings=mappings,
                error=APIError(
                    message="Allowed domain already exists with different settings",
                    code="DOMAIN_ALREADY_EXISTS",
                ),
            )
        return MutationResult(
            status=MutationStatus.DRY_RUN if dry_run else MutationStatus.OK,
            endpoint=endpoint,
            dry_run=dry_run,
            current=existing,
            desired=desired,
            readback=existing,
            resolved_mappings=mappings,
            warnings=["Allowed domain already exists with the requested configuration"],
        )
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            desired=desired,
            request=desired,
            changed_fields=["domain", "allow_subdomains"],
            resolved_mappings=mappings,
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="POST", request=desired,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.desired = desired
        result.request = desired
        result.resolved_mappings = mappings
        return result
    try:
        readback = _find_named_object(
            _configuration_items(
                get_domains_configuration(), "domains", "Allowed-domain",
            ),
            "domain",
            normalized,
            "allowed domain",
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            desired=desired,
            request=desired,
            response=response,
            resolved_mappings=mappings,
            error=error,
        )
    failed = readback is None or readback.get("allow_subdomains") != allow_subdomains
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=endpoint,
        dry_run=False,
        desired=desired,
        request=desired,
        response=response,
        readback=readback,
        changed_fields=["domain", "allow_subdomains"],
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=["Allowed-domain readback did not match"] if failed else [],
    )


def update_allowed_domain(
    realm_url: str,
    domain: str,
    allow_subdomains: bool,
    expected_allow_subdomains: bool | None = None,
    dry_run: bool = False,
) -> MutationResult:
    normalized = domain.strip().lower()
    resolved_endpoint = f"/realm/domains/{urllib.parse.quote(normalized, safe='')}"
    server, failure = _owner_destination(resolved_endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    try:
        current_domain = _find_named_object(
            _configuration_items(
                get_domains_configuration(), "domains", "Allowed-domain",
            ),
            "domain",
            normalized,
            "allowed domain",
        )
        if current_domain is None:
            raise ValueError(f"Unknown allowed domain: {normalized}")
    except ZulipAPIError as exc:
        return _mutation_failure(resolved_endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=resolved_endpoint,
            dry_run=dry_run,
            desired={"allow_subdomains": allow_subdomains},
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    current_value = current_domain.get("allow_subdomains")
    current = {"allow_subdomains": current_value}
    desired = {"allow_subdomains": allow_subdomains}
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "domain": {"semantic": domain, "resolved": normalized},
    }
    if expected_allow_subdomains is not None and current_value != expected_allow_subdomains:
        result = _conflict_result(
            resolved_endpoint, dry_run, current, desired, ["allow_subdomains"], [],
        )
        result.resolved_mappings = mappings
        return result
    request: dict[str, JSONValue] = (
        {} if current_value == allow_subdomains
        else {"allow_subdomains": allow_subdomains}
    )
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=desired,
            request=request,
            changed_fields=list(request),
            resolved_mappings=mappings,
        )
    if not request:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=desired,
            readback=current,
            resolved_mappings=mappings,
            warnings=["Allowed domain already had the desired configuration"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = desired
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback = _find_named_object(
            _configuration_items(
                get_domains_configuration(), "domains", "Allowed-domain",
            ),
            "domain",
            normalized,
            "allowed domain",
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=desired,
            request=request,
            response=response,
            changed_fields=["allow_subdomains"],
            resolved_mappings=mappings,
            error=error,
        )
    failed = readback is None or readback.get("allow_subdomains") != allow_subdomains
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=desired,
        request=request,
        response=response,
        readback=(
            {"allow_subdomains": readback.get("allow_subdomains")}
            if readback is not None else {}
        ),
        changed_fields=["allow_subdomains"],
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=["Allowed-domain readback did not match"] if failed else [],
    )


def _linkifier_value(field: str, value: JSONValue) -> JSONValue:
    if field in LINKIFIER_REVERSE_FIELDS and value in (None, ""):
        return None
    return value


def create_linkifier(
    realm_url: str,
    pattern: str,
    url_template: str,
    reverse: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/filters"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    reverse = dict(reverse or {})
    invalid = set(reverse) - LINKIFIER_REVERSE_FIELDS
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    null_fields = sorted(field for field, value in reverse.items() if value is None)
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            error=APIError(
                message="Use an empty string, not null, to clear reverse-linkifier fields",
                code="NULL_WRITE_VALUE",
            ),
        )
    feature_level = server.get("zulip_feature_level")
    if reverse and (not isinstance(feature_level, int) or feature_level < 471):
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            unsupported_fields=sorted(reverse),
            error=APIError(
                message="Reverse linkifiers require Zulip feature level 471",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    desired = {"pattern": pattern, "url_template": url_template, **reverse}
    try:
        linkifiers = _configuration_items(
            get_linkifiers_configuration(), "linkifiers", "Linkifier",
        )
        existing = _find_named_object(linkifiers, "pattern", pattern, "linkifier pattern")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=desired,
            error=APIError(message=str(exc), code="INVENTORY_ERROR"),
        )
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
    }
    if existing is not None:
        mismatches = [
            field for field, value in desired.items()
            if (
                field not in existing and field not in LINKIFIER_REVERSE_FIELDS
            ) or _linkifier_value(field, existing.get(field)) != _linkifier_value(field, value)
        ]
        if mismatches:
            return MutationResult(
                status=MutationStatus.CONFLICT,
                endpoint=endpoint,
                dry_run=dry_run,
                current=existing,
                desired=desired,
                resolved_mappings=mappings,
                error=APIError(
                    message=f"Linkifier already exists with different settings: {', '.join(mismatches)}",
                    code="LINKIFIER_ALREADY_EXISTS",
                ),
            )
        return MutationResult(
            status=MutationStatus.DRY_RUN if dry_run else MutationStatus.OK,
            endpoint=endpoint,
            dry_run=dry_run,
            current=existing,
            desired=desired,
            readback=existing,
            resolved_mappings=mappings,
            warnings=["Linkifier already exists with the requested configuration"],
        )
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=endpoint,
            dry_run=True,
            desired=desired,
            request=desired,
            changed_fields=sorted(desired),
            resolved_mappings=mappings,
        )
    try:
        response = administrative_mutation_request(
            endpoint, method="POST", request=desired,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(endpoint, dry_run, exc)
        result.desired = desired
        result.request = desired
        result.resolved_mappings = mappings
        return result
    try:
        readback_items = _configuration_items(
            get_linkifiers_configuration(), "linkifiers", "Linkifier",
        )
        linkifier_id = response.get("id")
        readback = next(
            (
                item for item in readback_items
                if isinstance(linkifier_id, int) and item.get("id") == linkifier_id
            ),
            None,
        )
        if readback is None:
            readback = _find_named_object(
                readback_items, "pattern", pattern, "linkifier pattern",
            )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=endpoint,
            dry_run=False,
            desired=desired,
            request=desired,
            response=response,
            resolved_mappings=mappings,
            error=error,
        )
    failed = [
        field for field, value in desired.items()
        if readback is None
        or _linkifier_value(field, readback.get(field)) != _linkifier_value(field, value)
    ]
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=endpoint,
        dry_run=False,
        desired=desired,
        request=desired,
        response=response,
        readback=readback,
        changed_fields=sorted(desired),
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=["Linkifier readback did not match"] if failed else [],
    )


def update_linkifier(
    realm_url: str,
    pattern: str,
    changes: dict[str, JSONValue],
    expected: dict[str, JSONValue] | None = None,
    dry_run: bool = False,
) -> MutationResult:
    endpoint = "/realm/filters/{filter_id}"
    server, _, failure = _admin_destination(endpoint, realm_url, dry_run)
    if failure is not None:
        return failure
    assert server is not None
    expected = dict(expected or {})
    allowed = {"pattern", "url_template"} | set(LINKIFIER_REVERSE_FIELDS)
    invalid = (set(changes) | set(expected)) - allowed
    if invalid:
        return _invalid_fields_result(endpoint, dry_run, invalid)
    if not changes:
        return _invalid_fields_result(endpoint, dry_run, {"<no changes>"}, "EMPTY_CHANGES")
    null_fields = sorted(key for key, value in changes.items() if value is None)
    null_fields.extend(sorted(
        key for key, value in expected.items()
        if value is None and key not in LINKIFIER_REVERSE_FIELDS
    ))
    if null_fields:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(
                message="Use an empty string, not null, to clear reverse-linkifier fields",
                code="NULL_WRITE_VALUE",
            ),
        )
    feature_level = server.get("zulip_feature_level")
    reverse_fields = (set(changes) | set(expected)) & LINKIFIER_REVERSE_FIELDS
    if reverse_fields and (
        not isinstance(feature_level, int) or feature_level < 471
    ):
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            unsupported_fields=sorted(reverse_fields),
            error=APIError(
                message="Reverse linkifiers require Zulip feature level 471",
                code="UNSUPPORTED_FEATURE",
            ),
        )
    try:
        linkifiers = _configuration_items(
            get_linkifiers_configuration(), "linkifiers", "Linkifier",
        )
        current_linkifier = _find_named_object(
            linkifiers, "pattern", pattern, "linkifier pattern",
        )
        if current_linkifier is None:
            raise ValueError(f"Unknown linkifier pattern: {pattern}")
        filter_id = current_linkifier.get("id")
        if not isinstance(filter_id, int) or isinstance(filter_id, bool):
            raise ValueError("Target linkifier did not have a valid id")
    except ZulipAPIError as exc:
        return _mutation_failure(endpoint, dry_run, exc)
    except ValueError as exc:
        return MutationResult(
            status=MutationStatus.ERROR,
            endpoint=endpoint,
            dry_run=dry_run,
            desired=changes,
            error=APIError(message=str(exc), code="SEMANTIC_RESOLUTION_ERROR"),
        )
    missing = sorted(
        field for field in set(changes) | set(expected)
        if field not in current_linkifier and field not in LINKIFIER_REVERSE_FIELDS
    )
    if missing:
        return MutationResult(
            status=MutationStatus.UNSUPPORTED,
            endpoint=endpoint,
            dry_run=dry_run,
            current=current_linkifier,
            desired=changes,
            unsupported_fields=missing,
            error=APIError(message="Linkifier response omitted requested fields", code="UNSUPPORTED_FIELD"),
        )
    mismatches = sorted(
        field for field, value in expected.items()
        if _linkifier_value(field, current_linkifier.get(field))
        != _linkifier_value(field, value)
    )
    current = {
        field: current_linkifier.get(field) for field in set(changes) | set(expected)
    }
    if mismatches:
        return _conflict_result(endpoint, dry_run, current, changes, mismatches, [])
    changed = sorted(
        field for field, value in changes.items()
        if _linkifier_value(field, current_linkifier.get(field))
        != _linkifier_value(field, value)
    )
    request: dict[str, JSONValue] = {
        "pattern": changes.get("pattern", current_linkifier.get("pattern")),
        "url_template": changes.get(
            "url_template", current_linkifier.get("url_template"),
        ),
    }
    request.update({field: changes[field] for field in changed if field in LINKIFIER_REVERSE_FIELDS})
    resolved_endpoint = f"/realm/filters/{filter_id}"
    mappings: dict[str, JSONValue] = {
        "realm_url": {"semantic": realm_url, "resolved": server.get("realm_url")},
        "linkifier": {"semantic": pattern, "resolved": filter_id},
    }
    if dry_run:
        return MutationResult(
            status=MutationStatus.DRY_RUN,
            endpoint=resolved_endpoint,
            dry_run=True,
            current=current,
            desired=changes,
            request=request if changed else {},
            changed_fields=changed,
            resolved_mappings=mappings,
        )
    if not changed:
        return MutationResult(
            status=MutationStatus.OK,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            readback=current,
            resolved_mappings=mappings,
            warnings=["Linkifier already had the desired configuration"],
        )
    try:
        response = administrative_mutation_request(
            resolved_endpoint, method="PATCH", request=request,
        )
    except ZulipAPIError as exc:
        result = _mutation_failure(resolved_endpoint, dry_run, exc)
        result.current = current
        result.desired = changes
        result.request = request
        result.resolved_mappings = mappings
        return result
    try:
        readback = next(
            (
                item for item in _configuration_items(
                    get_linkifiers_configuration(), "linkifiers", "Linkifier",
                ) if item.get("id") == filter_id
            ),
            None,
        )
    except (ZulipAPIError, ValueError) as exc:
        error = exc.error if isinstance(exc, ZulipAPIError) else APIError(
            message=str(exc), code="READBACK_ERROR",
        )
        return MutationResult(
            status=MutationStatus.PARTIAL,
            endpoint=resolved_endpoint,
            dry_run=False,
            current=current,
            desired=changes,
            request=request,
            response=response,
            changed_fields=changed,
            resolved_mappings=mappings,
            error=error,
        )
    failed = [
        field for field, value in changes.items()
        if readback is None
        or _linkifier_value(field, readback.get(field)) != _linkifier_value(field, value)
    ]
    unsupported = _ignored_parameters(response)
    return MutationResult(
        status=MutationStatus.PARTIAL if failed or unsupported else MutationStatus.OK,
        endpoint=resolved_endpoint,
        dry_run=False,
        current=current,
        desired=changes,
        request=request,
        response=response,
        readback=(
            {field: readback.get(field) for field in changes}
            if readback is not None else {}
        ),
        changed_fields=changed,
        resolved_mappings=mappings,
        unsupported_fields=unsupported,
        warnings=["Linkifier readback did not match"] if failed else [],
    )


def get_user_email(full_name: str) -> Optional[str]:
    """Look up a user's email by their full name."""
    client = get_client()
    result = client.get_users()
    if result["result"] != "success":
        return None
    for user in result["members"]:
        if user["full_name"] == full_name:
            return user["email"]
    return None


def resolve_name(query: str) -> list[dict]:
    """Find active users whose display name contains the query (case-insensitive).

    Returns list of dicts with 'full_name' and 'email' for each match.
    """
    client = get_client()
    result = client.get_users()
    if result["result"] != "success":
        return []
    q = query.lower()
    matches = []
    for user in result["members"]:
        if user.get("is_bot", False) or not user.get("is_active", True):
            continue
        if q in user["full_name"].lower():
            matches.append({"full_name": user["full_name"], "email": user["email"]})
    matches.sort(key=lambda u: u["full_name"])
    return matches


# ============================================================================
# Bot visibility filtering — /nobots and /nb support
# ============================================================================

_NOBOTS_KEYWORDS = ("/nobots", "/nb")


def _should_hide_from_bot(msg: dict) -> bool:
    """Check if a message should be hidden from bots.

    Returns True if:
    - The topic contains '/nobots' or '/nb' (case-insensitive)
    - The message content starts with '/nobots' or '/nb' (case-insensitive, after stripping)
    """
    topic = msg.get("subject", "").lower()
    if any(kw in topic for kw in _NOBOTS_KEYWORDS):
        return True
    content = msg.get("content", "").strip().lower()
    if any(content.startswith(kw) for kw in _NOBOTS_KEYWORDS):
        return True
    return False


def filter_for_bot(messages: list[dict]) -> list[dict]:
    """Filter out messages that should be hidden from bots.

    Use this before displaying messages to the bot or checking if new messages exist.
    """
    return [m for m in messages if not _should_hide_from_bot(m)]


def _format_timestamp(timestamp: int, prev_timestamp: Optional[int] = None) -> str:
    """Smart timestamp formatting.

    - Always shows date on the first message
    - Shows date only when it changes from the previous message
    - Shows time only when there's a 5+ minute gap from the previous message
    - Uses the configured timezone
    """
    dt = datetime.fromtimestamp(timestamp, tz=TIMEZONE)
    show_date = True
    show_time = True

    if prev_timestamp is not None:
        prev_dt = datetime.fromtimestamp(prev_timestamp, tz=TIMEZONE)
        show_date = dt.date() != prev_dt.date()
        gap_minutes = (timestamp - prev_timestamp) / 60
        show_time = gap_minutes >= 5 or show_date

    if show_date and show_time:
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    elif show_time:
        return dt.strftime("%H:%M:%S %Z")
    else:
        return ""


def _time_attr(msg: dict, prev_timestamp: Optional[int]) -> str:
    """Build the time attribute string for a <msg> tag."""
    timestamp = msg.get("timestamp", 0)
    if "time_range_end" in msg:
        start = datetime.fromtimestamp(timestamp, tz=TIMEZONE)
        end = datetime.fromtimestamp(msg["time_range_end"], tz=TIMEZONE)
        if start.date() != end.date():
            return f'time="{start.strftime("%Y-%m-%d %H:%M:%S %Z")}-{end.strftime("%Y-%m-%d %H:%M:%S %Z")}"'
        return f'time="{start.strftime("%Y-%m-%d %H:%M:%S %Z")}-{end.strftime("%H:%M:%S %Z")}"'
    ts_str = _format_timestamp(timestamp, prev_timestamp)
    if ts_str:
        return f'time="{ts_str}"'
    return ""


def get_messages(hours_back: int = 24, channels: Optional[list[str]] = None,
                 sender: Optional[str] = None,
                 as_of: Optional[datetime] = None) -> list[dict]:
    """General-purpose paginated fetch returning raw message dicts sorted by ID.

    channels=None → all public streams
    channels=["eng", "ops"] → specific streams
    sender="John Dean" → auto-resolves name to email, filters by sender
    as_of=datetime(2025,6,1) → treat this as "now" (fetch hours_back before it)
    """
    cache_key = ("get_messages", hours_back, tuple(channels) if channels else None,
                 sender, as_of.isoformat() if as_of else None)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    client = get_client()
    anchor_time = int(as_of.timestamp()) if as_of else int(time.time())
    cutoff = anchor_time - (hours_back * 3600)

    narrow = []
    if channels is None:
        narrow.append({"operator": "channels", "operand": "public"})
    # If specific channels given, we fetch per-channel and merge
    if sender:
        email = get_user_email(sender)
        if not email:
            return []
        narrow.append({"operator": "sender", "operand": email})

    if channels is not None:
        # Fetch from each channel separately and merge
        all_messages = []
        for channel in channels:
            channel_narrow = narrow + [{"operator": "channel", "operand": channel}]
            msgs = _paginated_fetch(client, channel_narrow, cutoff, anchor_time)
            all_messages.extend(msgs)
        # Deduplicate by id and sort
        seen = set()
        unique = []
        for m in all_messages:
            if m["id"] not in seen:
                seen.add(m["id"])
                unique.append(m)
        unique.sort(key=lambda m: m["id"])
        _cache.set(cache_key, unique, expire=600)
        return unique
    else:
        result = _paginated_fetch(client, narrow, cutoff, anchor_time)
        _cache.set(cache_key, result, expire=600)
        return result


def _paginated_fetch(client: zulip.Client, narrow: list[dict],
                     cutoff: int, upper_bound: Optional[int] = None) -> list[dict]:
    """Paginate backward from newest in batches of 500 until time cutoff."""
    cache_key = ("_paginated_fetch", tuple(tuple(d.items()) for d in narrow), cutoff, upper_bound)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    all_messages = []
    anchor = "newest"
    while True:
        result = client.get_messages({
            "anchor": anchor,
            "num_before": 500,
            "num_after": 0,
            "narrow": narrow,
            "apply_markdown": False,
        })
        if result["result"] != "success":
            break
        batch = result["messages"]
        if not batch:
            break

        found_old = False
        for msg in batch:
            if upper_bound and msg["timestamp"] > upper_bound:
                continue
            if msg["timestamp"] >= cutoff:
                all_messages.append(msg)
            else:
                found_old = True

        if found_old:
            break

        # Move anchor to oldest message in this batch
        anchor = batch[0]["id"]
        # If we got fewer than requested, we've hit the beginning
        if len(batch) < 500:
            break

    all_messages.sort(key=lambda m: m["id"])
    if _ignored_streams:
        all_messages = [
            m for m in all_messages
            if m.get("display_recipient", "").lower() not in _ignored_streams
        ]
    _cache.set(cache_key, all_messages, expire=600)
    return all_messages


def format_messages(messages: list[dict], include_topic: bool = False,
                    combine: bool = True) -> str:
    """Format messages as XML <msg> tags.

    combine=True merges consecutive messages from same user within 2 min.
    Smart timestamps: date shown only when it changes, time shown on 5+ min gaps.
    include_topic=True adds stream and topic attributes to each <msg> tag.

    Note: Messages in /nobots or /nb topics or starting with /nobots or /nb are automatically
    filtered out and will not be shown to the bot.
    """
    if not messages:
        return ""

    # Filter out messages that should be hidden from bots
    messages = filter_for_bot(messages)
    if not messages:
        return ""

    if combine:
        messages = _combine_messages(messages)

    lines = []
    prev_timestamp = None
    for msg in messages:
        sender = msg.get("sender_full_name", "Unknown")
        msg_id = msg.get("id", "")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", 0)

        # Build the time attribute
        time_attr = _time_attr(msg, prev_timestamp)

        attrs = [f'user="{sender}"']
        if time_attr:
            attrs.append(time_attr)
        if include_topic and msg.get("type") == "stream":
            stream_name = msg.get("display_recipient", "")
            visibility = "private" if is_stream_private(stream_name) else "public"
            attrs.append(f'stream="{stream_name}"')
            attrs.append(f'visibility="{visibility}"')
            attrs.append(f'topic="{msg.get("subject", "")}"')
        attrs.append(f'id="{msg_id}"')

        # Add reaction summary as attribute if present
        reaction_str = summarize_reactions(msg.get("reactions", []))
        if reaction_str:
            attrs.append(f'reactions="{reaction_str}"')

        tag_open = "<msg " + " ".join(attrs) + ">"
        lines.append(f"{tag_open}\n{content}\n</msg>")

        prev_timestamp = msg.get("time_range_end", timestamp)

    return "\n".join(lines)


def _combine_messages(messages: list[dict]) -> list[dict]:
    """Merge consecutive messages from same user within 2 minutes."""
    if not messages:
        return []

    combined = []
    current = dict(messages[0])
    current["reactions"] = list(current.get("reactions", []))

    for msg in messages[1:]:
        same_user = msg.get("sender_full_name") == current.get("sender_full_name")
        time_gap = msg.get("timestamp", 0) - current.get("time_range_end", current.get("timestamp", 0))

        if same_user and time_gap <= 120:
            # Merge: append content, update end time, id, and reactions
            current["content"] = current["content"] + "\n\n" + msg.get("content", "")
            current["time_range_end"] = msg.get("timestamp", 0)
            current["id"] = msg.get("id", current["id"])
            current["reactions"].extend(msg.get("reactions", []))
        else:
            combined.append(current)
            current = dict(msg)
            current["reactions"] = list(current.get("reactions", []))

    combined.append(current)
    return combined


def _context_window(messages: list[dict], sender_email: str) -> list[dict]:
    """Keep messages around where sender participated.

    Before each user message: 3 messages OR 10 min (whichever captures more).
    After each user message: 3 messages OR (2 hours AND <10 messages).
    """
    if not messages:
        return []

    include = set()
    sender_indices = [i for i, m in enumerate(messages)
                      if m.get("sender_email") == sender_email]

    for idx in sender_indices:
        include.add(idx)
        ts = messages[idx]["timestamp"]

        # Before: 3 messages or 10 min
        for i in range(idx - 1, -1, -1):
            if (idx - i) <= 3 or (ts - messages[i]["timestamp"]) <= 600:
                include.add(i)
            else:
                break

        # After: 3 messages or (2hr and <10 messages)
        for i in range(idx + 1, len(messages)):
            dist = i - idx
            if dist <= 3 or (messages[i]["timestamp"] - ts <= 7200 and dist < 10):
                include.add(i)
            else:
                break

    return [messages[i] for i in sorted(include)]


def format_topics(messages: list[dict], combine: bool = True) -> str:
    """Group messages by stream>topic, format each group.

    Topics sorted by last message ID. Groups separated by ---.
    """
    if not messages:
        return ""

    # Group by (stream, topic)
    groups: dict[tuple[str, str], list[dict]] = {}
    for msg in messages:
        stream = msg.get("display_recipient", "unknown")
        topic = msg.get("subject", "unknown")
        key = (stream, topic)
        if key not in groups:
            groups[key] = []
        groups[key].append(msg)

    # Sort groups by last message ID
    sorted_groups = sorted(groups.items(), key=lambda item: item[1][-1]["id"])

    sections = []
    for (stream, topic), group_msgs in sorted_groups:
        visibility = "[private]" if is_stream_private(stream) else "[public]"
        header = f"Stream: {visibility} {stream}, Topic: {topic}"
        body = format_messages(group_msgs, include_topic=False, combine=combine)
        sections.append(f"{header}\n{body}")

    return "\n---\n".join(sections)


def get_messages_formatted(hours_back: int = 24, channels: Optional[list[str]] = None,
                           sender: Optional[str] = None,
                           group_by_topic: bool = True,
                           as_of: Optional[datetime] = None) -> str:
    """Convenience: fetch + format in one call.

    When sender is set with group_by_topic=True, fetches full conversation
    context around the sender's messages in each topic they participated in.
    as_of=datetime(2025,6,1) → treat this as "now" (fetch hours_back before it)
    """
    if sender and group_by_topic:
        return _get_sender_conversations(hours_back, channels, sender, as_of)

    messages = get_messages(hours_back=hours_back, channels=channels, sender=sender, as_of=as_of)
    if not messages:
        return "No messages found."
    if group_by_topic:
        return format_topics(messages)
    else:
        return format_messages(messages, include_topic=True)


def _get_sender_conversations(hours_back: int, channels: Optional[list[str]],
                              sender: str, as_of: Optional[datetime] = None) -> str:
    """Two-pass fetch: find sender's topics, then get full context."""
    client = get_client()
    email = get_user_email(sender)
    if not email:
        return "No messages found."

    # Pass 1: get sender's messages to discover topics
    sender_messages = get_messages(hours_back=hours_back, channels=channels, sender=sender, as_of=as_of)
    if not sender_messages:
        return "No messages found."

    # Extract unique (stream, topic) pairs
    topics = set()
    for msg in sender_messages:
        if msg.get("type") == "stream":
            topics.add((msg["display_recipient"], msg["subject"]))

    # Pass 2: fetch all messages from each topic, apply context window
    anchor_time = int(as_of.timestamp()) if as_of else int(time.time())
    cutoff = anchor_time - (hours_back * 3600)
    all_context_messages = []
    for stream, topic in topics:
        narrow = [
            {"operator": "channel", "operand": stream},
            {"operator": "topic", "operand": topic},
        ]
        topic_messages = _paginated_fetch(client, narrow, cutoff, anchor_time)
        windowed = _context_window(topic_messages, email)
        all_context_messages.extend(windowed)

    if not all_context_messages:
        return "No messages found."

    return format_topics(all_context_messages)


# ============================================================================
# Direct API wrappers — return raw Python objects for composability
# ============================================================================

def get_profile() -> dict:
    """Get the bot's own profile. Returns the profile dict from the API."""
    return get_client().get_profile()


def list_streams(include_private: bool = False) -> list[dict]:
    """List available streams. Returns list of stream dicts."""
    result = get_client().get_streams(include_public=True, include_subscribed=True)
    if result["result"] != "success":
        return []
    streams = result.get("streams", [])
    allowed = _allowed_private_streams()
    if _ALL_PRIVATE_STREAMS not in allowed:
        streams = [
            s for s in streams
            if not s.get("invite_only", False) or s["name"].lower() in allowed
        ]
    if not include_private:
        streams = [s for s in streams if not s.get("invite_only", False)]
    return sorted(streams, key=lambda s: s["name"])


def get_stream_topics(stream: str, limit: int = 20) -> list[dict]:
    """Get recent topics in a stream. Returns list of topic dicts.

    Note: Topics containing '/nobots' or '/nb' are filtered out and will not be shown to bots.
    """
    if not is_private_stream_allowed(stream):
        raise ValueError(f"Private stream access denied: {stream}")
    client = get_client()
    result = client.get_stream_id(stream)
    if result["result"] != "success":
        raise ValueError(f"Stream '{stream}' not found: {result.get('msg', '')}")
    result = client.get_stream_topics(result["stream_id"])
    if result["result"] != "success":
        raise ValueError(f"Error fetching topics: {result.get('msg', '')}")
    topics = result.get("topics", [])
    # Filter out /nobots and /nb topics
    topics = [t for t in topics
              if not any(kw in t.get("name", "").lower() for kw in _NOBOTS_KEYWORDS)]
    return topics[:limit]


def get_topic_messages(stream: str, topic: str, num_messages: int = 20,
                       before_message_id: Optional[int] = None) -> list[dict]:
    """Get messages from a specific stream/topic. Returns raw message dicts sorted by ID."""
    if not is_private_stream_allowed(stream):
        return []
    result = get_client().get_messages({
        "narrow": [
            {"operator": "stream", "operand": stream},
            {"operator": "topic", "operand": topic},
        ],
        "anchor": before_message_id or "newest",
        "num_before": min(num_messages, 100),
        "num_after": 0,
        "apply_markdown": False,
    })
    if result["result"] != "success":
        return []
    msgs = result.get("messages", [])
    msgs.sort(key=lambda m: m["id"])
    return msgs


def fetch_new_messages(stream: str, topic: str, after_id: int,
                       exclude_user_id: Optional[int] = None) -> list[dict]:
    """Fetch messages after a given ID, optionally excluding a user. Sorted by ID."""
    if not is_private_stream_allowed(stream):
        return []
    result = get_client().get_messages({
        "narrow": [
            {"operator": "stream", "operand": stream},
            {"operator": "topic", "operand": topic},
        ],
        "anchor": after_id,
        "num_before": 0,
        "num_after": 100,
        "include_anchor": False,
        "apply_markdown": False,
    })
    if result["result"] != "success":
        return []
    msgs = result.get("messages", [])
    if exclude_user_id:
        msgs = [m for m in msgs if m.get("sender_id") != exclude_user_id]
    msgs.sort(key=lambda m: m["id"])
    return msgs


def send_message(stream: str, topic: str, content: str) -> dict:
    """Send a message. Returns API result dict with 'id' on success."""
    err = _stream_write_error(stream)
    if err:
        return err
    return get_client().send_message({
        "type": "stream",
        "to": stream,
        "subject": topic,
        "content": normalize_zulip_markdown(content),
    })


def send_direct_message(recipients: list[str], content: str) -> dict:
    """Send a direct message (DM) to one or more users.

    Args:
        recipients: List of email addresses to send to.
        content: Message content (supports Zulip markdown).

    Returns:
        API result dict with 'id' on success.
    """
    return get_client().send_message({
        "type": "direct",
        "to": recipients,
        "content": normalize_zulip_markdown(content),
    })


def add_reaction(message_id: int, emoji_name: str) -> dict:
    """Add emoji reaction to a message. Returns API result dict."""
    return get_client().add_reaction({
        "message_id": message_id,
        "emoji_name": emoji_name,
    })


def remove_reaction(message_id: int, emoji_name: str,
                     reaction_type: Optional[str] = None) -> dict:
    """Remove emoji reaction from a message. Returns API result dict.

    Pass *reaction_type* (``"unicode_emoji"``, ``"realm_emoji"``, or
    ``"zulip_extra_emoji"``) for non-unicode emoji — the server defaults
    to ``"unicode_emoji"`` when omitted, so custom-emoji removals fail
    without it.
    """
    params: dict = {
        "message_id": message_id,
        "emoji_name": emoji_name,
    }
    if reaction_type is not None:
        params["reaction_type"] = reaction_type
    return get_client().remove_reaction(params)


def edit_message(message_id: int, content: str) -> dict:
    """Edit a message's content. Returns API result dict."""
    _, err = _get_stream_message_for_write(message_id)
    if err:
        return err
    return get_client().update_message({
        "message_id": message_id,
        "content": normalize_zulip_markdown(content),
    })


def move_messages(message_id: int, topic: str, stream: Optional[str] = None,
                  propagate_mode: str = "change_one", notify: bool = True) -> dict:
    """Move message(s) to a different topic and/or stream.

    Uses the Zulip update_message API with topic/stream_id params.

    Args:
        message_id: The anchor message to move.
        topic: Destination topic name.
        stream: Destination stream name (optional, only for cross-channel moves).
        propagate_mode: "change_one", "change_later", or "change_all".
        notify: Send notifications to old and new threads (default True).
            Set to False for silent renames like topic resolution.

    Returns:
        API result dict.
    """
    _, err = _get_stream_message_for_write(message_id)
    if err:
        return err

    request: dict = {
        "message_id": message_id,
        "topic": topic,
        "propagate_mode": propagate_mode,
        "send_notification_to_old_thread": notify,
        "send_notification_to_new_thread": notify,
    }
    if stream:
        err = _stream_write_error(stream)
        if err:
            return err
        client = get_client()
        result = client.get_stream_id(stream)
        if result["result"] != "success":
            return result
        request["stream_id"] = result["stream_id"]
    return get_client().update_message(request)


_stream_id_cache: dict[str, int] = {}


def send_typing(stream: str, topic: str, op: str = "start") -> dict:
    """Send a typing indicator start/stop to a stream/topic.

    Args:
        stream: Stream name.
        topic: Topic name.
        op: "start" or "stop".

    Returns API result dict.
    """
    client = get_client()
    stream_id = _stream_id_cache.get(stream)
    if stream_id is None:
        stream_result = client.get_stream_id(stream)
        if stream_result["result"] != "success":
            return stream_result
        stream_id = stream_result["stream_id"]
        _stream_id_cache[stream] = stream_id
    return client.call_endpoint(
        url="/typing",
        method="POST",
        request={
            "op": op,
            "type": "stream",
            "stream_id": stream_id,
            "topic": topic,
        },
    )


def list_emoji(query: str = "") -> tuple[list[str], int]:
    """List custom emoji, optionally filtered by substring.

    Returns (matching_names, total_count).
    """
    client = get_client()
    result = client.get_realm_emoji()
    if result.get("result") != "success":
        return [], 0
    all_emoji = sorted(
        info["name"]
        for info in result.get("emoji", {}).values()
        if not info.get("deactivated", False)
    )
    total = len(all_emoji)
    if query:
        q = query.lower()
        return [name for name in all_emoji if q in name.lower()], total
    return all_emoji, total


def get_emoji_info() -> tuple[int, set[str]]:
    """Return (count, names) of active custom emoji.

    Single API call — use this instead of separate count/has checks.
    Returns ``(0, set())`` on error.
    """
    try:
        result = get_client().get_realm_emoji()
        if result.get("result") != "success":
            return 0, set()
        names = {
            info["name"]
            for info in result.get("emoji", {}).values()
            if not info.get("deactivated", False)
        }
        return len(names), names
    except Exception:
        return 0, set()


def get_emoji_count() -> int:
    """Return count of active custom emoji. Returns 0 on error."""
    count, _ = get_emoji_info()
    return count


def summarize_reactions(reactions: list) -> str:
    """Summarize a list of Zulip reaction dicts into a compact string.

    Returns e.g. ":thumbs_up: x2  :check:" or empty string if none.
    """
    counts: dict[str, int] = {}
    for r in reactions:
        name = r.get("emoji_name", "")
        if name:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return ""
    return "  ".join(
        f":{name}: x{n}" if n > 1 else f":{name}:"
        for name, n in counts.items()
    )


def check_reactions_on(message_id: int) -> str:
    """Fetch a single message by ID and return its reaction summary, or empty string."""
    try:
        result = get_client().call_endpoint(
            url=f"/messages/{message_id}",
            method="GET",
        )
        if result.get("result") != "success":
            return ""
        msg = result.get("message", {})
        reaction_str = summarize_reactions(msg.get("reactions", []))
        if not reaction_str:
            return ""
        return f"Reactions on message {message_id}: {reaction_str}"
    except Exception:
        return ""


def discover_message_context(message_id: int) -> Optional[tuple[str, str]]:
    """Look up a message by ID and return its (stream, topic), or None for DMs/errors."""
    result = get_client().get_messages({
        "anchor": message_id,
        "num_before": 0,
        "num_after": 0,
        "include_anchor": True,
        "apply_markdown": False,
    })
    if result.get("result") != "success" or not result.get("messages"):
        return None
    target = result["messages"][0]
    if target.get("type") != "stream":
        return None
    stream = target.get("display_recipient", "")
    topic = target.get("subject", "")
    if not stream or not topic:
        return None
    return stream, topic


def get_custom_profile_fields() -> list[dict]:
    """Get the org's custom profile field definitions. Cached aggressively."""
    cache_key = ("custom_profile_fields",)
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached
    result = get_client().call_endpoint(url="/realm/profile_fields", method="GET")
    if result.get("result") != "success":
        return []
    fields = result.get("custom_fields", [])
    _cache.set(cache_key, fields, expire=3600)
    return fields


def get_user_info(email: str) -> Optional[dict]:
    """Get user info by email, including custom profile fields. Returns user dict or None."""
    result = get_client().call_endpoint(
        url=f"/users/{email}",
        method="GET",
        request={"include_custom_profile_fields": True},
    )
    if result.get("result") != "success":
        return None
    return result["user"]


def get_message_by_id(message_id: int) -> Optional[dict]:
    """Get a specific message by ID. Returns message dict or None.

    Uses get_messages with apply_markdown=False so the content field contains
    raw markdown (consistent with get_topic_messages), not rendered HTML.
    """
    result = get_client().get_messages({
        "anchor": message_id,
        "num_before": 0,
        "num_after": 0,
        "include_anchor": True,
        "apply_markdown": False,
    })
    if result.get("result") != "success" or not result.get("messages"):
        return None
    msg = result["messages"][0]
    if msg.get("type") == "stream":
        stream = msg.get("display_recipient", "")
        if stream and not is_private_stream_allowed(stream):
            return None
    return msg


def verify_message(message_id: int) -> str:
    """Fetch a message by ID and return it in a secure, tamper-evident format.

    The sender identity comes directly from the Zulip API and cannot be
    spoofed by message content. All '#' and '@' characters are stripped from
    the message body so that the structural delimiters (##### lines and
    @FIELD labels) cannot be forged within the content.

    Returns a formatted string with verified metadata and sanitized content,
    or an error string if the message cannot be fetched.
    """
    msg = get_message_by_id(message_id)
    if not msg:
        return f"Error: Message {message_id} not found."

    sender_name = msg.get("sender_full_name", "Unknown")
    sender_email = msg.get("sender_email", "Unknown")
    sender_id = msg.get("sender_id", "Unknown")
    timestamp = msg.get("timestamp", 0)
    ts_str = datetime.fromtimestamp(timestamp, tz=TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z") if timestamp else "Unknown"
    stream = msg.get("display_recipient", "Unknown") if msg.get("type") == "stream" else "DM"
    topic = msg.get("subject", "Unknown") if msg.get("type") == "stream" else "N/A"

    # Strip # and @ so delimiters can't be forged in content
    content = msg.get("content", "")
    content = content.replace("#", "").replace("@", "")

    return (
        f"##### VERIFIED MESSAGE BEGIN #####\n"
        f"@SENDER NAME: {sender_name}\n"
        f"@SENDER EMAIL: {sender_email}\n"
        f"@SENDER USER ID: {sender_id}\n"
        f"@TIMESTAMP: {ts_str}\n"
        f"@STREAM: {stream}\n"
        f"@TOPIC: {topic}\n"
        f"@MESSAGE ID: {message_id}\n"
        f"##### CONTENT #####\n"
        f"{content}\n"
        f"##### VERIFIED MESSAGE END #####"
    )


_hash_replacements = {"%": ".", "(": ".28", ")": ".29", ".": ".2E"}


def _encode_hash_component(s: str) -> str:
    """Encode a string for Zulip URL hash fragments (MediaWiki-style dot-encoding)."""
    encoded = urllib.parse.quote(s, safe="")
    return "".join(_hash_replacements.get(c, c) for c in encoded)


def get_message_link(message_id: int) -> str:
    """Return a Zulip markdown link to a message: [#stream > topic](url).

    The URL links to the full conversation context with the specific message
    focused, using the format:
        {base}/#narrow/channel/{stream_id}-{stream_slug}/topic/{topic_encoded}/near/{message_id}

    Falls back to a simpler URL format if stream_id lookup fails.
    """
    client = get_client()
    base = client.base_url.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]

    msg = get_message_by_id(message_id)
    if not msg:
        url = f"{base}/#narrow/id/{message_id}"
        return f"[message]({url})"

    stream = msg.get("display_recipient", "")
    topic = msg.get("subject", "")

    stream_id = None
    try:
        result = client.get_stream_id(stream)
        if result.get("result") == "success":
            stream_id = result["stream_id"]
    except Exception:
        pass

    if stream_id:
        topic_encoded = _encode_hash_component(topic)
        stream_slug = _encode_hash_component(stream.replace(" ", "-"))
        url = f"{base}/#narrow/channel/{stream_id}-{stream_slug}/topic/{topic_encoded}/near/{message_id}"
    else:
        url = f"{base}/#narrow/id/{message_id}"

    return f"[#{stream} > {topic}]({url})"


def get_subscribed_streams() -> list[dict]:
    """Get streams the bot is subscribed to. Returns list of subscription dicts."""
    result = get_client().get_subscriptions()
    if result["result"] != "success":
        return []
    subs = result.get("subscriptions", [])
    allowed = _allowed_private_streams()
    if allowed is not None:
        subs = [
            s for s in subs
            if not s.get("invite_only", False) or s["name"].lower() in allowed
        ]
    return sorted(subs, key=lambda s: s["name"])


def get_stream_members(stream: str) -> list[dict]:
    """Get members of a stream. Returns list of user dicts with 'full_name' and 'email'."""
    if not is_private_stream_allowed(stream):
        raise ValueError(f"Private stream access denied: {stream}")
    client = get_client()
    result = client.get_stream_id(stream)
    if result["result"] != "success":
        raise ValueError(f"Stream '{stream}' not found: {result.get('msg', '')}")
    stream_id = result["stream_id"]
    result = client.call_endpoint(url=f"/streams/{stream_id}/members", method="GET")
    if result.get("result") != "success":
        raise ValueError(f"Error fetching members: {result.get('msg', '')}")
    # Resolve user IDs to names/emails
    user_ids = set(result.get("subscribers", []))
    users_result = client.get_users()
    if users_result["result"] != "success":
        return []
    members = []
    for user in users_result["members"]:
        if user["user_id"] in user_ids:
            members.append({"full_name": user["full_name"], "email": user["email"]})
    return sorted(members, key=lambda u: u["full_name"])


def download_file(path: str) -> tuple[bytes, str]:
    """Download a file from Zulip. Returns (content_bytes, content_type).

    Raises ValueError on any download error.
    """
    client = get_client()
    client.ensure_session()
    if client.session is None:
        raise ValueError("Failed to create Zulip session")

    base = client.base_url.rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    if path.startswith(("https://", "http://")):
        url = path
        base_origin = urllib.parse.urlsplit(base)
        url_origin = urllib.parse.urlsplit(url)
        authenticated = (
            base_origin.scheme == url_origin.scheme
            and base_origin.netloc == url_origin.netloc
        )
    else:
        if not path.startswith("/"):
            path = "/" + path
        url = base + path
        authenticated = True

    response = (
        client.session.get(url, timeout=30)
        if authenticated
        else requests.get(url, timeout=30)
    )
    if response.status_code == 403:
        raise ValueError(f"Access denied (403). The bot may not have permission to access: {path}")
    elif response.status_code == 404:
        raise ValueError(f"File not found (404): {path}")
    elif response.status_code != 200:
        raise ValueError(f"HTTP {response.status_code}: {response.text[:200]}")
    content_type = response.headers.get("Content-Type", "application/octet-stream")
    return response.content, content_type


def save_file(path: str, save_dir: Optional[str] = None) -> tuple[str, int, str]:
    """Download a Zulip file and save it locally.

    Returns (saved_path, size_bytes, content_type).
    """
    content, content_type = download_file(path)
    filename = Path(path).name

    if save_dir:
        save_path = Path(save_dir) / filename
        save_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        save_path = Path(tempfile.mkdtemp()) / filename

    save_path.write_bytes(content)
    return str(save_path), len(content), content_type


def save_image(path: str) -> str:
    """Download a Zulip image and save to a temp file. Returns the temp file path."""
    content, content_type = download_file(path)

    ext_map = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
        "image/webp": ".webp", "image/svg+xml": ".svg",
    }
    ext = ext_map.get((content_type or "").split(";")[0].strip(), "")
    if not ext:
        ext = Path(path).suffix.lower() if Path(path).suffix.lower() in ext_map.values() else ".bin"

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
        f.write(content)
        return f.name


def upload_file(file_path: str) -> tuple[str, str]:
    """Upload a local file to Zulip.

    Args:
        file_path: Absolute path to the file to upload.

    Returns:
        Tuple of (uri, filename) where uri is the Zulip upload path like
        "/user_uploads/2/ab/cdef/image.png".

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the upload fails.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    client = get_client()
    with open(path, "rb") as f:
        result = client.upload_file(f)

    if result.get("result") != "success":
        raise ValueError(f"Upload failed: {result.get('msg', 'Unknown error')}")

    return result["uri"], path.name


# ============================================================================
# Event queue — long-polling for real-time message delivery
# ============================================================================

def is_bot_subscribed(stream: str) -> bool:
    """Check if the bot is currently subscribed to a stream."""
    result = get_client().get_subscriptions()
    if result.get("result") != "success":
        return False
    return any(sub["name"].lower() == stream.lower()
               for sub in result.get("subscriptions", []))


def ensure_subscribed(stream: str) -> bool:
    """Subscribe the bot to a stream. No-op if already subscribed.

    For public streams, auto-subscribes. For private streams (or streams
    the bot can't see), returns True only if already subscribed.
    """
    # Fast path: already subscribed
    if is_bot_subscribed(stream):
        return True
    # Private streams can't be joined — must be invited
    if is_stream_private(stream):
        return False
    # Try to subscribe (works for public streams)
    result = get_client().add_subscriptions(
        streams=[{"name": stream}],
    )
    return result.get("result") == "success"


def register_event_queue(stream: str, topic: str) -> tuple[str, int, int]:
    """Register an event queue narrowed to a stream/topic.

    Returns (queue_id, last_event_id, longpoll_timeout_seconds).
    Raises ValueError on failure.
    """
    result = get_client().call_endpoint(
        url="/register",
        method="POST",
        request={
            "event_types": json.dumps(["message", "reaction"]),
            "narrow": json.dumps([
                ["channel", stream],
                ["topic", topic],
            ]),
            "apply_markdown": False,
        },
    )
    if result.get("result") != "success":
        raise ValueError(f"Failed to register event queue: {result.get('msg', '')}")
    timeout = result.get("event_queue_longpoll_timeout_seconds", 90)
    return result["queue_id"], result["last_event_id"], timeout


def get_events(queue_id: str, last_event_id: int,
               longpoll_timeout: int = 90) -> tuple[list[dict], list[dict], int]:
    """Long-poll for events. Blocks until events arrive or server heartbeat.

    Returns (messages, reactions, new_last_event_id). Lists may be empty
    (heartbeat only). Raises ValueError on BAD_EVENT_QUEUE_ID (caller
    should re-register).
    """
    try:
        result = get_client().call_endpoint(
            url="/events",
            method="GET",
            request={
                "queue_id": queue_id,
                "last_event_id": last_event_id,
            },
            timeout=longpoll_timeout + 30,  # HTTP timeout > server timeout
        )
    except requests.exceptions.ReadTimeout:
        return [], [], last_event_id
    if result.get("result") != "success":
        code = result.get("code", "")
        if code == "BAD_EVENT_QUEUE_ID":
            raise ValueError("BAD_EVENT_QUEUE_ID")
        raise ValueError(f"get_events failed: {result.get('msg', '')}")
    events = result.get("events", [])
    messages = []
    reactions = []
    new_last = last_event_id
    for ev in events:
        new_last = max(new_last, ev.get("id", new_last))
        if ev.get("type") == "message":
            messages.append(ev["message"])
        elif ev.get("type") == "reaction":
            reactions.append(ev)
    return messages, reactions, new_last


def delete_event_queue(queue_id: str) -> None:
    """Delete an event queue. Ignores errors (best-effort cleanup)."""
    try:
        get_client().call_endpoint(
            url="/events",
            method="DELETE",
            request={"queue_id": queue_id},
        )
    except Exception:
        pass
