from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, TypeAlias


JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
REDACTED = "[REDACTED]"

_SECRET_KEYS = frozenset({
    "api_key", "apikey", "password", "passwd", "secret", "token",
    "authorization", "proxy_authorization", "cookie", "set_cookie",
    "credentials", "config_data", "invite_link", "invite_url",
    "invitation_link", "invitation_url", "webhook_url", "webhook_key",
})
_SECRET_SUFFIXES = (
    "_api_key", "_password", "_secret", "_token", "_credentials",
    "_config_data", "_invitation_link", "_invitation_url", "_invite_link",
    "_invite_url", "_webhook_url", "_webhook_key",
)
_TEXT_KEYS = frozenset({
    "detail", "diagnostic", "error", "errors", "message", "msg", "reason",
    "warning", "warnings",
})
_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?P<quote>[\"']?)(?P<key>[A-Za-z][A-Za-z0-9_.-]*)"
    r"(?P=quote)\s*[:=]\s*)"
)
_AUTH_RE = re.compile(r"\b(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+", re.I)
_INVITE_URL_RE = re.compile(
    r"https?://[^\s<>\"']+/(?:join|invite|invites)/[^\s<>\"']+", re.I
)
_URL_CREDENTIAL_RE = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@", re.I)
_QUERY_SECRET_RE = re.compile(r"([?&])([^?&=\s]+)=([^&#\s<>\"']*)")
_FORBIDDEN_MESSAGE_RE = re.compile(
    r"\b(?:must be|only) (?:an? )?(?:organization )?(?:administrator|owner)s?\b"
    r"|\b(?:insufficient permission|not authorized|permission denied)\b"
    r"|\bdo(?:es)? not have permission\b",
    re.I,
)
_FORBIDDEN_CODES = frozenset({
    "FORBIDDEN", "INSUFFICIENT_PERMISSION", "INVALID_API_KEY",
    "PERMISSION_DENIED", "REALM_DEACTIVATED", "UNAUTHORIZED",
    "USER_DEACTIVATED",
})
_UNSUPPORTED_CODES = frozenset({"UNSUPPORTED_FEATURE", "UNSUPPORTED_OPERATION"})


def _secret_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = normalized.lower().replace("-", "_").replace(".", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _text_key(key: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = normalized.lower().replace("-", "_").replace(".", "_")
    return (
        normalized in _TEXT_KEYS
        or normalized.endswith("_message")
        or normalized.endswith("_error")
    )


def sanitize_text(value: str, secrets: Iterable[str] = ()) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            if len(secret) < 8:
                value = re.sub(
                    rf"(?<![\w]){re.escape(secret)}(?![\w])",
                    REDACTED,
                    value,
                )
            else:
                value = value.replace(secret, REDACTED)
    value = _AUTH_RE.sub(REDACTED, value)
    value = _INVITE_URL_RE.sub(REDACTED, value)
    value = _URL_CREDENTIAL_RE.sub(r"\1" + REDACTED + "@", value)
    value = _QUERY_SECRET_RE.sub(
        lambda match: (
            f"{match[1]}{match[2]}={REDACTED}"
            if _secret_key(match[2]) else match[0]
        ),
        value,
    )
    decoder = json.JSONDecoder()
    position = 0
    while match := _ASSIGNMENT_RE.search(value, position):
        start = match.end()
        position = start
        if not _secret_key(match["key"]):
            continue
        try:
            parsed, length = decoder.raw_decode(value[start:])
            remainder = value[start + length:start + length + 1]
            if remainder and not remainder.isspace() and remainder not in ",;}]":
                raise ValueError
        except ValueError:
            parsed = None
            if value[start:start + 1] in "[{":
                length = len(value) - start
            else:
                raw = re.match(
                    r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|[^\s,;}\]]+",
                    value[start:],
                )
                length = len(raw[0]) if raw else 0
        if length and value[start:start + length] != "null":
            replacement = json.dumps(REDACTED) if isinstance(parsed, str) else REDACTED
            value = value[:start] + replacement + value[start + length:]
            position = start + len(replacement)
    return value


def _collect_secrets(value: JSONValue) -> set[str]:
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(key):
                if isinstance(item, str) and item:
                    secrets.add(item)
                else:
                    secrets.update(_collect_secrets(item))
            else:
                secrets.update(_collect_secrets(item))
    elif isinstance(value, list):
        for item in value:
            secrets.update(_collect_secrets(item))
    return secrets


def redact_secrets(value: JSONValue) -> JSONValue:
    secrets = _collect_secrets(value)

    def redact(item: JSONValue, text_context: bool = False) -> JSONValue:
        if isinstance(item, dict):
            return {
                key: (
                    REDACTED
                    if _secret_key(key)
                    and child is not None
                    and not isinstance(child, bool)
                    else redact(child, _text_key(key))
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child, text_context) for child in item]
        if isinstance(item, str):
            return sanitize_text(item, secrets if text_context else ())
        return item

    return redact(value)


class SectionStatus(str, Enum):
    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    FORBIDDEN = "forbidden"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class MutationStatus(str, Enum):
    DISABLED = "disabled"
    DRY_RUN = "dry_run"
    CONFLICT = "conflict"
    OK = "ok"
    PARTIAL = "partial"
    FORBIDDEN = "forbidden"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


@dataclass(frozen=True)
class APIError:
    message: str
    code: str | None = None
    http_status: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", sanitize_text(self.message))
        if self.code is not None:
            object.__setattr__(self, "code", sanitize_text(self.code))

    @classmethod
    def from_response(
        cls, response: dict[str, JSONValue], http_status: int | None = None,
    ) -> APIError:
        sanitized = redact_secrets(response)
        assert isinstance(sanitized, dict)
        message = sanitized.get("msg")
        code = sanitized.get("code")
        status = http_status if http_status is not None else sanitized.get("status_code")
        return cls(
            message=message if isinstance(message, str) else "Zulip API request failed",
            code=code if isinstance(code, str) else None,
            http_status=status if isinstance(status, int) and not isinstance(status, bool) else None,
        )

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "message": self.message,
            "code": self.code,
            "http_status": self.http_status,
        }


class ZulipAPIError(Exception):
    def __init__(self, error: APIError) -> None:
        self.error = error
        super().__init__(error.message)


@dataclass
class SectionResult:
    status: SectionStatus
    data: JSONValue = None
    absent_fields: list[str] = field(default_factory=list)
    unsupported_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: APIError | None = None

    def __post_init__(self) -> None:
        self.status = SectionStatus(self.status)

    def to_dict(self) -> dict[str, JSONValue]:
        result = redact_secrets({
            "status": self.status.value,
            "data": self.data,
            "absent_fields": list(self.absent_fields),
            "unsupported_fields": list(self.unsupported_fields),
            "warnings": list(self.warnings),
            "error": self.error.to_dict() if self.error is not None else None,
        })
        assert isinstance(result, dict)
        return result


@dataclass
class MutationResult:
    status: MutationStatus
    endpoint: str
    dry_run: bool
    current: dict[str, JSONValue] = field(default_factory=dict)
    desired: dict[str, JSONValue] = field(default_factory=dict)
    request: dict[str, JSONValue] = field(default_factory=dict)
    response: dict[str, JSONValue] | None = None
    readback: dict[str, JSONValue] | None = None
    changed_fields: list[str] = field(default_factory=list)
    resolved_mappings: dict[str, JSONValue] = field(default_factory=dict)
    unsupported_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: APIError | None = None
    steps: list[dict[str, JSONValue]] = field(default_factory=list)
    completed_fields: list[str] = field(default_factory=list)
    remaining_fields: list[str] = field(default_factory=list)
    failed_step: str | None = None

    def __post_init__(self) -> None:
        self.status = MutationStatus(self.status)

    def to_dict(self) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {
            "status": self.status.value,
            "endpoint": self.endpoint,
            "dry_run": self.dry_run,
            "current": self.current,
            "desired": self.desired,
            "request": self.request,
            "response": self.response,
            "readback": self.readback,
            "changed_fields": self.changed_fields,
            "resolved_mappings": self.resolved_mappings,
            "unsupported_fields": self.unsupported_fields,
            "warnings": self.warnings,
            "error": self.error.to_dict() if self.error is not None else None,
        }
        if self.steps:
            value["steps"] = self.steps
        if self.completed_fields:
            value["completed_fields"] = self.completed_fields
        if self.remaining_fields:
            value["remaining_fields"] = self.remaining_fields
        if self.failed_step is not None:
            value["failed_step"] = self.failed_step
        result = redact_secrets(value)
        assert isinstance(result, dict)
        return result


CURRENT_USER_FIELDS = (
    "user_id", "full_name", "email", "role", "is_owner", "is_admin", "is_guest",
    "is_bot", "is_active", "bot_type", "bot_owner_id",
)

CONFIGURATION_FETCH_EVENT_TYPES = (
    "realm",
    "realm_user_settings_defaults",
    "default_streams",
    "default_stream_groups",
)

USER_AUDIT_FIELDS = (
    "user_id", "full_name", "email", "role", "is_owner", "is_admin",
    "is_guest", "is_bot", "is_active", "bot_type", "bot_owner_id",
)
BOT_AUDIT_FIELDS = USER_AUDIT_FIELDS + (
    "default_sending_stream",
    "default_events_register_stream",
    "default_all_public_streams",
)
ORGANIZATION_SECTIONS = (
    "profile",
    "branding",
    "authentication",
    "access",
    "permissions",
    "message_policies",
    "new_user_defaults",
    "default_channels",
    "groups",
    "profile_fields",
    "domains",
    "linkifiers",
    "emoji",
    "users",
    "bots",
    "invitations",
)

REALM_WRITE_FIELDS = frozenset({
    "allow_message_editing", "authentication_methods",
    "avatar_changes_disabled", "can_access_all_users_group",
    "can_add_custom_emoji_group", "can_add_subscribers_group",
    "can_create_bots_group", "can_create_groups",
    "can_create_private_channel_group", "can_create_public_channel_group",
    "can_create_web_public_channel_group", "can_create_write_only_bots_group",
    "can_delete_any_message_group", "can_delete_own_message_group",
    "can_invite_users_group", "can_manage_all_groups",
    "can_manage_billing_group", "can_mention_many_users_group",
    "can_move_messages_between_channels_group",
    "can_move_messages_between_topics_group", "can_resolve_topics_group",
    "can_set_delete_message_policy_group", "can_set_topics_policy_group",
    "can_summarize_topics_group", "create_multiuse_invite_group",
    "default_avatar_source", "default_code_block_language", "default_language",
    "description", "digest_emails_enabled", "digest_weekday",
    "direct_message_initiator_group", "direct_message_permission_group",
    "disallow_disposable_email_addresses", "email_changes_disabled",
    "emails_restricted_to_domains", "enable_guest_user_dm_warning",
    "enable_guest_user_indicator", "enable_read_receipts",
    "enable_spectator_access", "gif_rating_policy", "inline_image_preview",
    "inline_url_embed_preview", "invite_required", "jitsi_server_url",
    "media_preview_size", "message_content_allowed_in_email_notifications",
    "message_content_delete_limit_seconds", "message_content_edit_limit_seconds",
    "message_edit_history_visibility_policy", "message_retention_days",
    "moderation_request_channel_id", "move_messages_between_streams_limit_seconds",
    "move_messages_within_stream_limit_seconds", "name", "name_changes_disabled",
    "new_stream_announcements_stream_id", "org_type",
    "require_e2ee_push_notifications", "require_unique_names",
    "send_channel_events_messages", "send_welcome_emails",
    "signup_announcements_stream_id", "string_id", "topics_policy",
    "video_chat_provider", "waiting_period_threshold",
    "want_advertise_in_communities_directory", "welcome_message_custom_text",
    "workplace_users_group", "zulip_update_announcements_stream_id",
})

DEFAULT_USER_WRITE_FIELDS = frozenset({
    "automatically_follow_topics_policy", "automatically_follow_topics_where_mentioned",
    "automatically_unmute_topics_in_muted_streams_policy", "color_scheme",
    "demote_inactive_streams", "desktop_icon_count_display",
    "display_emoji_reaction_users", "email_address_visibility",
    "email_notifications_batching_period_seconds", "emojiset",
    "enable_desktop_notifications", "enable_digest_emails",
    "enable_drafts_synchronization", "enable_followed_topic_audible_notifications",
    "enable_followed_topic_desktop_notifications",
    "enable_followed_topic_email_notifications",
    "enable_followed_topic_push_notifications",
    "enable_followed_topic_wildcard_mentions_notify",
    "enable_offline_email_notifications", "enable_offline_push_notifications",
    "enable_online_push_notifications", "enable_sounds",
    "enable_stream_audible_notifications", "enable_stream_desktop_notifications",
    "enable_stream_email_notifications", "enable_stream_push_notifications",
    "enter_sends", "fluid_layout_width", "hide_ai_features", "high_contrast_mode",
    "left_side_userlist", "message_content_in_email_notifications",
    "notification_sound", "pm_content_in_desktop_notifications", "presence_enabled",
    "realm_name_in_email_notifications_policy", "receives_typing_notifications",
    "resolved_topic_notice_auto_read_policy", "send_private_typing_notifications",
    "send_read_receipts", "send_stream_typing_notifications",
    "starred_message_counts", "translate_emoticons", "twenty_four_hour_time",
    "user_list_style", "web_animate_image_previews", "web_channel_default_view",
    "web_escape_navigates_to_home_view", "web_font_size_px", "web_home_view",
    "web_inbox_show_channel_folders", "web_left_sidebar_show_channel_folders",
    "web_left_sidebar_unreads_count_summary", "web_line_height_percent",
    "web_mark_read_on_scroll_policy", "web_navigate_to_sent_message",
    "web_stream_unreads_count_display_policy", "web_suggest_update_timezone",
    "wildcard_mentions_notify",
})

OWNER_ONLY_REALM_FIELDS = frozenset({
    "authentication_methods", "disallow_disposable_email_addresses",
    "emails_restricted_to_domains", "invite_required", "message_retention_days",
    "waiting_period_threshold", "create_multiuse_invite_group", "can_create_groups",
    "can_invite_users_group", "can_manage_all_groups", "can_manage_billing_group",
    "string_id",
})

GROUP_SETTING_REALM_FIELDS = frozenset(
    field for field in REALM_WRITE_FIELDS if field.endswith("_group")
) | frozenset({"can_create_groups", "can_manage_all_groups"})

CHANNEL_REFERENCE_REALM_FIELDS = frozenset({
    "moderation_request_channel_id",
    "new_stream_announcements_stream_id",
    "signup_announcements_stream_id",
    "zulip_update_announcements_stream_id",
})

UNLIMITED_REALM_FIELDS = frozenset({
    "message_retention_days",
    "message_content_delete_limit_seconds",
    "message_content_edit_limit_seconds",
    "move_messages_between_streams_limit_seconds",
    "move_messages_within_stream_limit_seconds",
})

CHANNEL_GROUP_FIELDS = frozenset({
    "can_add_subscribers_group",
    "can_administer_channel_group",
    "can_create_topic_group",
    "can_delete_any_message_group",
    "can_delete_own_message_group",
    "can_move_messages_out_of_channel_group",
    "can_move_messages_within_channel_group",
    "can_remove_subscribers_group",
    "can_resolve_topics_group",
    "can_send_message_group",
    "can_subscribe_group",
})

CHANNEL_CREATE_FIELDS = frozenset({
    "announce",
    "is_default_stream",
    "history_public_to_subscribers",
    "message_retention_days",
    "topics_policy",
})

CHANNEL_CREATE_RESIDUAL_FIELDS = frozenset({
    "can_create_topic_group",
    "can_delete_any_message_group",
    "can_delete_own_message_group",
})

CHANNEL_UPDATE_FIELDS = frozenset({
    "new_name",
    "description",
    "privacy",
    "history_public_to_subscribers",
    "is_default_stream",
    "is_archived",
    "message_retention_days",
    "topics_policy",
}) | CHANNEL_GROUP_FIELDS

USER_GROUP_PERMISSION_FIELDS = frozenset({
    "can_add_members_group",
    "can_join_group",
    "can_leave_group",
    "can_manage_group",
    "can_mention_group",
    "can_remove_members_group",
})

USER_GROUP_UPDATE_FIELDS = frozenset({
    "name",
    "description",
}) | USER_GROUP_PERMISSION_FIELDS

PROFILE_FIELD_CREATE_FIELDS = frozenset({
    "name",
    "hint",
    "field_data",
    "required",
    "display_in_profile_summary",
    "editable_by_user",
    "use_for_user_matching",
})

PROFILE_FIELD_UPDATE_FIELDS = PROFILE_FIELD_CREATE_FIELDS

LINKIFIER_REVERSE_FIELDS = frozenset({
    "example_input",
    "reverse_template",
})
QUEUE_SECTIONS = frozenset({
    "profile",
    "branding",
    "authentication",
    "access",
    "permissions",
    "message_policies",
    "new_user_defaults",
    "default_channels",
})

_MESSAGE_POLICY_TERMS = (
    "retention", "message_content", "message_edit", "message_delet",
    "message_move", "move_messages", "edit_history", "topic",
    "read_receipt", "email_content", "wildcard_mention",
    "default_code_block_language", "automatically_follow",
)
_ACCESS_TERMS = (
    "invite", "domain", "registration", "account_creation", "web_public",
    "waiting_period", "new_user_announcements", "enable_spectator_access",
    "email_changes_disabled",
)


@dataclass
class ConfigurationQueueSnapshot:
    data: dict[str, JSONValue]
    warnings: list[str] = field(default_factory=list)


def read_section(
    reader: Callable[[], dict[str, JSONValue]],
    fields: tuple[str, ...] | None = None,
    collection_field: str | None = None,
) -> SectionResult:
    try:
        data = reader()
    except ZulipAPIError as exc:
        error = exc.error
        if (
            error.http_status in {401, 403}
            or error.code in _FORBIDDEN_CODES
            or _FORBIDDEN_MESSAGE_RE.search(error.message)
        ):
            status = SectionStatus.FORBIDDEN
        elif error.code in _UNSUPPORTED_CODES:
            status = SectionStatus.UNSUPPORTED
        else:
            status = SectionStatus.ERROR
        return SectionResult(status=status, error=error)
    absent_fields = []
    if fields is not None:
        absent_fields = [name for name in fields if name not in data]
        data = {name: data[name] for name in fields if name in data}
    status = SectionStatus.EMPTY if not data else SectionStatus.OK
    warnings: list[str] = []
    if collection_field is not None:
        if collection_field not in data:
            status = SectionStatus.PARTIAL
            absent_fields.append(collection_field)
            warnings.append(f"Upstream response omitted {collection_field}")
        elif data[collection_field] is None:
            status = SectionStatus.PARTIAL
            warnings.append(f"Upstream response returned null for {collection_field}")
        elif not data[collection_field]:
            status = SectionStatus.EMPTY
    return SectionResult(
        status=status,
        data=data,
        absent_fields=absent_fields,
        warnings=warnings,
    )


def project_user_inventory(
    response: dict[str, JSONValue],
    include_sensitive_user_fields: bool = False,
    include_deactivated: bool = False,
) -> dict[str, JSONValue]:
    members = response.get("members")
    if not isinstance(members, list):
        return response
    projected: list[JSONValue] = []
    for member in members:
        if (
            isinstance(member, dict)
            and member.get("is_active") is False
            and not include_deactivated
        ):
            continue
        if not isinstance(member, dict):
            projected.append(member)
        elif include_sensitive_user_fields:
            projected.append(redact_secrets(member))
        else:
            projected.append({
                field: member[field] for field in USER_AUDIT_FIELDS if field in member
            })
    return {**response, "members": projected}


def project_bot_inventory(
    response: dict[str, JSONValue], include_deactivated: bool = False,
) -> dict[str, JSONValue]:
    members = response.get("members")
    if "members" not in response:
        return response
    if members is None:
        return {
            key: value for key, value in response.items() if key != "members"
        } | {"bots": None}
    if not isinstance(members, list):
        return response
    bots: list[JSONValue] = []
    for member in members:
        if (
            isinstance(member, dict)
            and member.get("is_bot") is True
            and (include_deactivated or member.get("is_active") is not False)
        ):
            bots.append({field: member[field] for field in BOT_AUDIT_FIELDS if field in member})
    return {
        key: value for key, value in response.items() if key != "members"
    } | {"bots": bots}


def partition_realm_snapshot(
    snapshot: dict[str, JSONValue],
) -> dict[str, dict[str, JSONValue]]:
    partitioned = {
        "profile": {},
        "authentication": {},
        "access": {},
        "permissions": {},
        "message_policies": {},
    }
    excluded = {
        "last_event_id",
        "event_queue_longpoll_timeout_seconds",
        "realm_user_settings_defaults",
        "default_streams",
        "default_stream_groups",
        "realm_default_streams",
        "realm_default_stream_groups",
        "realm_icon_url",
        "realm_icon_source",
        "max_icon_file_size_mib",
        "realm_logo_url",
        "realm_logo_source",
        "realm_night_logo_url",
        "realm_night_logo_source",
        "max_logo_file_size_mib",
    }
    for key, value in snapshot.items():
        if key in excluded:
            continue
        normalized = key.removeprefix("realm_")
        if (
            normalized.startswith("can_")
            or normalized.endswith("_group")
            or key == "server_supported_permission_settings"
            or "stream_policy" in normalized
        ):
            section = "permissions"
        elif "authentication" in normalized or normalized.startswith("email_auth"):
            section = "authentication"
        elif any(term in normalized for term in _MESSAGE_POLICY_TERMS):
            section = "message_policies"
        elif any(term in normalized for term in _ACCESS_TERMS):
            section = "access"
        else:
            section = "profile"
        partitioned[section][key] = value

    retention = partitioned["message_policies"].get("realm_message_retention_days")
    if "realm_message_retention_days" in partitioned["message_policies"]:
        if retention in (None, -1):
            meaning = "retain_forever"
        else:
            meaning = "retain_for_days"
        partitioned["message_policies"]["retention_semantics"] = meaning
    return partitioned


def resolve_permission_groups(
    permissions: dict[str, JSONValue],
    groups: dict[int, str],
) -> dict[str, JSONValue]:
    resolved: dict[str, JSONValue] = {}
    for key, value in permissions.items():
        if not (key.removeprefix("realm_").startswith("can_") or key.endswith("_group")):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            resolved[key] = {
                "raw": value,
                "group_name": groups.get(value),
            }
        elif isinstance(value, dict):
            subgroups = value.get("direct_subgroups")
            names = []
            if isinstance(subgroups, list):
                names = [
                    groups.get(group_id)
                    for group_id in subgroups
                    if isinstance(group_id, int) and not isinstance(group_id, bool)
                ]
            resolved[key] = {
                "raw": value,
                "direct_subgroup_names": names,
            }
    return resolved


def annotate_channel_configuration(
    response: dict[str, JSONValue], groups: dict[int, str],
) -> dict[str, JSONValue]:
    streams = response.get("streams")
    if not isinstance(streams, list):
        return response
    annotated: list[JSONValue] = []
    for stream in streams:
        if not isinstance(stream, dict):
            annotated.append(stream)
            continue
        channel = dict(stream)
        if "message_retention_days" in channel:
            retention = channel["message_retention_days"]
            if retention is None:
                meaning = "inherit_realm_policy"
            elif retention == -1:
                meaning = "retain_forever"
            else:
                meaning = "retain_for_days"
            channel["retention_semantics"] = meaning
        resolved = resolve_permission_groups(channel, groups)
        if resolved:
            channel["resolved_group_settings"] = resolved
        annotated.append(channel)
    return {**response, "streams": annotated}
