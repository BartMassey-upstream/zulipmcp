import asyncio
import copy
import importlib
import json
from unittest.mock import Mock
from typing import Callable

import pytest
import requests
import zulip
from fastmcp.tools.tool import ToolResult
from requests.adapters import BaseAdapter

from zulipmcp import core
from zulipmcp.configuration import (
    CURRENT_USER_FIELDS,
    JSONValue,
    REDACTED,
    ZulipAPIError,
    read_section,
)

mcp_module = importlib.import_module("zulipmcp.mcp")


class StaticResponseAdapter(BaseAdapter):
    def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 403
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({
            "result": "error",
            "code": "BAD_REQUEST",
            "msg": "Nur Organisationsadministratoren",
        }).encode()
        return response

    def close(self) -> None:
        pass


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock(api_key="transport-private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    return client


@pytest.mark.parametrize(
    "reader,path",
    [(core.get_server_settings, "/server_settings"), (core.get_current_user, "/users/me")],
)
def test_identity_endpoints_preserve_typed_fields(
    client: Mock, reader: Callable[[], dict[str, JSONValue]], path: str,
) -> None:
    response = {
        "result": "success",
        "msg": "",
        "zulip_feature_level": 500,
        "authentication_methods": {"password": True, "ldap": False},
        "future_field": [None, {"nested": [1, False, "value"]}],
        "api_key": "credential-value",
    }
    unchanged = copy.deepcopy(response)
    client.call_endpoint.return_value = response

    data = reader()

    client.call_endpoint.assert_called_once_with(url=path, method="GET", request=None)
    assert data == {
        "zulip_feature_level": 500,
        "authentication_methods": {"password": True, "ldap": False},
        "future_field": [None, {"nested": [1, False, "value"]}],
        "api_key": REDACTED,
    }
    assert response == unchanged


def test_current_user_limits_output_and_preserves_absent_vs_null(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success", "msg": "", "user_id": 23,
        "full_name": "Audit Bot",
        "role": None, "is_admin": False, "is_bot": True, "bot_type": 1,
        "email": "audit@example.test", "delivery_email": "private@example.test",
        "profile_data": {"1": {"value": "private data"}},
        "api_key": "private-key", "timezone": "Europe/London",
        "services": [{"config_data": {"secret": "private-integration"}}],
    }

    result = mcp_module.get_current_user()

    assert isinstance(result, ToolResult)
    assert result.structured_content["data"] == {
        "user_id": 23, "full_name": "Audit Bot",
        "email": "audit@example.test", "role": None,
        "is_admin": False, "is_bot": True, "bot_type": 1,
    }
    assert result.structured_content["absent_fields"] == [
        "is_owner", "is_guest", "is_active", "bot_owner_id",
    ]
    assert result.content[0].text == "Current user: ok."
    assert "private" not in str(result)


@pytest.mark.parametrize(
    "code,http_status,expected",
    [
        ("PERMISSION_DENIED", None, "forbidden"),
        ("BAD_REQUEST", 403, "forbidden"),
        ("UNAUTHORIZED", 401, "forbidden"),
        ("INVALID_API_KEY", None, "forbidden"),
        ("UNSUPPORTED_FEATURE", None, "unsupported"),
        ("BAD_REQUEST", 400, "error"),
    ],
)
def test_identity_errors_are_classified_and_sanitized(
    client: Mock, code: str, http_status: int | None, expected: str,
) -> None:
    client.call_endpoint.return_value = {
        "result": "error", "code": code, "status_code": http_status,
        "msg": "request failed with api_key=private-secret",
    }

    result = mcp_module.get_server_settings()

    assert result.structured_content["status"] == expected
    assert result.structured_content["data"] is None
    assert result.structured_content["error"] == {
        "message": f"request failed with api_key={REDACTED}",
        "code": code, "http_status": http_status,
    }
    assert "private-secret" not in str(result)
    assert REDACTED in result.content[0].text


def test_permission_message_is_forbidden_without_http_status(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "error",
        "code": "BAD_REQUEST",
        "msg": "Must be an organization administrator",
    }

    result = mcp_module.get_server_settings()

    assert result.structured_content["status"] == "forbidden"
    assert result.structured_content["error"]["http_status"] is None


def test_sdk_response_status_classifies_translated_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = zulip.Client.__new__(zulip.Client)
    client.api_key = "private-key"
    client.base_url = "https://zulip.example.test/api/"
    client.has_connected = False
    client.retry_on_errors = False
    client.verbose = False
    client.session = requests.Session()
    client.session.mount("https://", StaticResponseAdapter())
    monkeypatch.setattr(core, "get_client", lambda: client)

    result = mcp_module.get_server_settings()

    assert result.structured_content["status"] == "forbidden"
    assert result.structured_content["error"] == {
        "message": "Nur Organisationsadministratoren",
        "code": "BAD_REQUEST",
        "http_status": 403,
    }


def test_transport_failure_masks_client_key(client: Mock) -> None:
    client.call_endpoint.side_effect = requests.ConnectionError(
        "request failed for transport-private-key",
    )

    result = read_section(core.get_current_user, CURRENT_USER_FIELDS).to_dict()

    assert result["status"] == "error"
    assert result["error"] == {
        "message": f"request failed for {REDACTED}",
        "code": "TRANSPORT_ERROR", "http_status": None,
    }


def test_unrecoverable_network_error_is_structured(client: Mock) -> None:
    client.call_endpoint.side_effect = zulip.UnrecoverableNetworkError(
        "cannot connect to server",
    )

    result = read_section(core.get_server_settings).to_dict()

    assert result["status"] == "error"
    assert result["error"] == {
        "message": "cannot connect to server",
        "code": "TRANSPORT_ERROR",
        "http_status": None,
    }


def test_api_failure_masks_echoed_client_key(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "error", "code": "UNAUTHORIZED",
        "msg": "invalid credential transport-private-key",
    }

    result = mcp_module.get_current_user()

    assert "transport-private-key" not in str(result)
    assert REDACTED in result.content[0].text


def test_invalid_api_response_is_not_empty_success(client: Mock) -> None:
    client.call_endpoint.return_value = []

    with pytest.raises(ZulipAPIError) as caught:
        core.get_server_settings()

    assert caught.value.error.code == "INVALID_RESPONSE"


def test_empty_success_is_explicit(client: Mock) -> None:
    client.call_endpoint.return_value = {"result": "success", "msg": ""}

    result = mcp_module.get_server_settings()

    assert result.structured_content["status"] == "empty"
    assert result.structured_content["data"] == {}
    assert result.structured_content["error"] is None


def test_fastmcp_delivers_real_structured_content(client: Mock) -> None:
    client.call_endpoint.return_value = {
        "result": "success", "msg": "", "zulip_feature_level": 500,
        "capabilities": {"nested": [True, None]},
    }

    result = asyncio.run(mcp_module.mcp.call_tool("get_server_settings", {}))

    assert result.structured_content["data"] == {
        "zulip_feature_level": 500, "capabilities": {"nested": [True, None]},
    }
    assert result.content[0].text == "Server settings: ok."
    assert "result" not in result.structured_content
