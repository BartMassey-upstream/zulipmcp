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


@dataclass
class ConfigurationQueueSnapshot:
    data: dict[str, JSONValue]
    warnings: list[str] = field(default_factory=list)


def read_section(
    reader: Callable[[], dict[str, JSONValue]],
    fields: tuple[str, ...] | None = None,
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
    return SectionResult(
        status=SectionStatus.OK if data else SectionStatus.EMPTY,
        data=data,
        absent_fields=absent_fields,
    )
