from zoneinfo import ZoneInfo

import pytest

from zulipmcp import core


def test_load_timezone_defaults_to_pacific(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZULIPMCP_TIMEZONE", raising=False)

    assert core._load_timezone() == ZoneInfo("America/Los_Angeles")


def test_load_timezone_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZULIPMCP_TIMEZONE", "Asia/Tokyo")

    assert core._load_timezone() == ZoneInfo("Asia/Tokyo")


def test_load_timezone_rejects_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZULIPMCP_TIMEZONE", "not-a-timezone")

    with pytest.raises(ValueError, match="Invalid ZULIPMCP_TIMEZONE"):
        core._load_timezone()


def test_format_timestamp_uses_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "TIMEZONE", ZoneInfo("Asia/Tokyo"))

    assert core._format_timestamp(0) == "1970-01-01 09:00:00 JST"


def test_time_range_uses_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "TIMEZONE", ZoneInfo("America/New_York"))
    message = {"timestamp": 0, "time_range_end": 300}

    assert (
        core._time_attr(message, None)
        == 'time="1969-12-31 19:00:00 EST-19:05:00 EST"'
    )


def test_verify_message_uses_configured_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "TIMEZONE", ZoneInfo("Asia/Tokyo"))
    monkeypatch.setattr(
        core,
        "get_message_by_id",
        lambda _message_id: {
            "sender_full_name": "Example User",
            "sender_email": "user@example.com",
            "sender_id": 42,
            "timestamp": 1,
            "type": "stream",
            "display_recipient": "general",
            "subject": "test",
            "content": "hello",
        },
    )

    assert "@TIMESTAMP: 1970-01-01 09:00:01 JST" in core.verify_message(1)
