import copy

import pytest

from zulipmcp.configuration import (
    APIError,
    REDACTED,
    SectionResult,
    SectionStatus,
    ZulipAPIError,
    redact_secrets,
    sanitize_text,
)


def test_section_status_values_are_stable() -> None:
    assert [status.value for status in SectionStatus] == [
        "ok",
        "empty",
        "partial",
        "forbidden",
        "unsupported",
        "error",
    ]


def test_section_result_preserves_null_and_records_absence() -> None:
    result = SectionResult(
        status=SectionStatus.OK,
        data={"retention_days": None},
        absent_fields=["is_default"],
        unsupported_fields=["default_push_notifications"],
    ).to_dict()

    assert result["data"] == {"retention_days": None}
    assert result["absent_fields"] == ["is_default"]
    assert result["unsupported_fields"] == ["default_push_notifications"]


def test_section_result_accepts_string_status() -> None:
    result = SectionResult(status="partial", warnings=["limited visibility"])

    assert result.status is SectionStatus.PARTIAL
    assert result.to_dict()["warnings"] == ["limited visibility"]


def test_section_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        SectionResult(status="unknown")


def test_redact_secrets_is_recursive_and_non_mutating() -> None:
    original = {
        "api_key": "top-secret-key",
        "nested": {
            "authorization": "Bearer credential-value",
            "config_data": {"signing_secret": "nested-secret"},
        },
        "items": [
            {"webhook_url": "https://hooks.example.test/private"},
            {"invite_link": "https://chat.example.test/join/secret-code"},
        ],
        "message": "request failed for top-secret-key",
    }
    unchanged = copy.deepcopy(original)

    redacted = redact_secrets(original)

    assert original == unchanged
    assert redacted == {
        "api_key": REDACTED,
        "nested": {
            "authorization": REDACTED,
            "config_data": REDACTED,
        },
        "items": [
            {"webhook_url": REDACTED},
            {"invite_link": REDACTED},
        ],
        "message": f"request failed for {REDACTED}",
    }


def test_redact_secrets_preserves_null_secret_fields() -> None:
    assert redact_secrets({"api_key": None}) == {"api_key": None}


def test_redact_secrets_preserves_non_secret_fields() -> None:
    payload = {
        "token_count": 42,
        "linkifier_url": "https://tracker.example.test/TICKET-1",
        "public_url": "https://chat.example.test/help",
    }

    assert redact_secrets(payload) == payload


def test_nested_config_data_does_not_corrupt_unrelated_strings() -> None:
    payload = {
        "status": "partial",
        "data": {
            "config_data": {"mode": "a"},
            "channel_name": "general",
        },
    }

    assert redact_secrets(payload) == {
        "status": "partial",
        "data": {
            "config_data": REDACTED,
            "channel_name": "general",
        },
    }


def test_nested_secret_is_redacted_when_echoed_in_message() -> None:
    payload = {
        "config_data": {"api_key": "nested-key", "mode": "production"},
        "msg": "nested-key is invalid",
    }

    assert redact_secrets(payload) == {
        "config_data": REDACTED,
        "msg": f"{REDACTED} is invalid",
    }


def test_short_secret_does_not_corrupt_envelope_fields() -> None:
    result = SectionResult(
        status="partial",
        data={"token": "a", "channel_name": "general"},
        warnings=["credential a is invalid"],
    ).to_dict()

    assert result["status"] == "partial"
    assert result["data"] == {
        "token": REDACTED,
        "channel_name": "general",
    }
    assert result["warnings"] == [
        f"credential {REDACTED} is invalid"
    ]


def test_sanitize_text_redacts_headers_urls_and_assignments() -> None:
    text = (
        "Authorization: Bearer abc.def; "
        "password='hunter2'; "
        "url=https://user:pass@example.test/path; "
        "invite=https://chat.example.test/join/private-code; "
        "callback=https://example.test/?access_token=private"
    )

    sanitized = sanitize_text(text)

    assert "abc.def" not in sanitized
    assert "hunter2" not in sanitized
    assert "user:pass" not in sanitized
    assert "private-code" not in sanitized
    assert "access_token=private" not in sanitized


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("api_key=123abcsecret", "abcsecret"),
        (r"api_key='abc\'def'", "def"),
    ],
)
def test_sanitize_text_redacts_complete_assignment_value(
    text: str, leaked: str,
) -> None:
    sanitized = sanitize_text(text)

    assert REDACTED in sanitized
    assert leaked not in sanitized


def test_sanitize_text_redacts_unparseable_secret_container() -> None:
    text = (
        "configuration failed: "
        "config_data={'secret': 'hunter2', 'other': 'tokenvalue'}"
    )

    sanitized = sanitize_text(text)

    assert sanitized == f"configuration failed: config_data={REDACTED}"
    assert "hunter2" not in sanitized
    assert "tokenvalue" not in sanitized


def test_api_error_sanitizes_response_and_exception_text() -> None:
    error = APIError.from_response({
        "result": "error",
        "code": "BAD_REQUEST",
        "msg": "Rejected Bearer private-token",
        "api_key": "private-token",
        "status_code": 403,
    })

    assert error.to_dict() == {
        "message": f"Rejected Bearer {REDACTED}",
        "code": "BAD_REQUEST",
        "http_status": 403,
    }
    assert "private-token" not in str(ZulipAPIError(error))


def test_section_result_redacts_error_and_data_together() -> None:
    result = SectionResult(
        status=SectionStatus.ERROR,
        data={"token": "shared-secret"},
        error=APIError(message="failed with token=shared-secret"),
    ).to_dict()

    assert result["data"] == {"token": REDACTED}
    assert "shared-secret" not in str(result["error"])
