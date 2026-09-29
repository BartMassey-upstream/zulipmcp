import asyncio
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import CONFIGURATION_FETCH_EVENT_TYPES, APIError, ZulipAPIError


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


def register_response(**fields: object) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "queue_id": "queue-1",
        "zulip_feature_level": 500,
        **fields,
    }


def delete_response() -> dict[str, object]:
    return {"result": "success", "msg": ""}


def assert_register_and_delete(client: Mock) -> None:
    assert client.call_endpoint.call_args_list == [
        call(
            url="/register",
            method="POST",
            request={
                "event_types": [],
                "fetch_event_types": list(CONFIGURATION_FETCH_EVENT_TYPES),
            },
        ),
        call(
            url="/events",
            method="DELETE",
            request={"queue_id": "queue-1"},
        ),
    ]


def test_configuration_queue_deletes_after_success(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        register_response(realm_name="Synthetic realm"),
        delete_response(),
    ]

    with core.configuration_queue_snapshot() as snapshot:
        assert snapshot.data == {
            "zulip_feature_level": 500,
            "realm_name": "Synthetic realm",
        }
        assert snapshot.warnings == []

    assert snapshot.warnings == []
    assert_register_and_delete(client)


def test_configuration_queue_deletes_after_upstream_processing_error(client: Mock) -> None:
    client.call_endpoint.side_effect = [register_response(), delete_response()]

    with pytest.raises(ZulipAPIError):
        with core.configuration_queue_snapshot():
            raise ZulipAPIError(APIError("section failed", "BAD_REQUEST"))

    assert_register_and_delete(client)


def test_configuration_queue_deletes_after_parse_error(client: Mock) -> None:
    client.call_endpoint.side_effect = [register_response(), delete_response()]

    with pytest.raises(ValueError, match="synthetic parse failure"):
        with core.configuration_queue_snapshot():
            raise ValueError("synthetic parse failure")

    assert_register_and_delete(client)


def test_configuration_queue_deletes_after_cancellation(client: Mock) -> None:
    client.call_endpoint.side_effect = [register_response(), delete_response()]

    with pytest.raises(asyncio.CancelledError):
        with core.configuration_queue_snapshot():
            raise asyncio.CancelledError

    assert_register_and_delete(client)


def test_configuration_queue_rejects_missing_queue_id(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "realm_name": "Synthetic realm",
    }

    with pytest.raises(ZulipAPIError) as caught:
        with core.configuration_queue_snapshot():
            pytest.fail("invalid registration must not yield")

    assert caught.value.error.code == "INVALID_RESPONSE"
    assert client.call_endpoint.call_count == 1


def test_configuration_queue_reports_cleanup_failure(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        register_response(),
        {"result": "error", "code": "BAD_REQUEST", "msg": "delete failed"},
    ]

    with core.configuration_queue_snapshot() as snapshot:
        pass

    assert snapshot.warnings == [
        "Failed to delete configuration event queue: delete failed",
    ]


def test_cleanup_failure_preserves_processing_error(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        register_response(),
        {"result": "error", "code": "BAD_REQUEST", "msg": "delete failed"},
    ]
    snapshot = None

    with pytest.raises(ValueError, match="processing failed"):
        with core.configuration_queue_snapshot() as snapshot:
            raise ValueError("processing failed")

    assert snapshot is not None
    assert snapshot.warnings == [
        "Failed to delete configuration event queue: delete failed",
    ]


def test_cleanup_failure_warning_and_log_are_sanitized(
    client: Mock, caplog: pytest.LogCaptureFixture,
) -> None:
    client.call_endpoint.side_effect = [
        register_response(),
        {
            "result": "error",
            "code": "BAD_REQUEST",
            "msg": "delete failed api_key=cleanup-secret",
        },
    ]

    with core.configuration_queue_snapshot() as snapshot:
        pass

    assert "cleanup-secret" not in str(snapshot.warnings)
    assert "cleanup-secret" not in caplog.text
    assert "[REDACTED]" in snapshot.warnings[0]
    assert "[REDACTED]" in caplog.text


def test_configuration_queue_accepts_already_deleted_queue(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        register_response(),
        {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "msg": "unknown queue"},
    ]

    with core.configuration_queue_snapshot() as snapshot:
        pass

    assert snapshot.warnings == []
