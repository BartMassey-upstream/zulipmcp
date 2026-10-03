import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")

REALM_URL = "https://realm.example.test"


def server(feature_level: int = 500) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "realm_url": REALM_URL,
        "zulip_feature_level": feature_level,
    }


def principal() -> dict[str, object]:
    return {
        "result": "success", "msg": "", "user_id": 1,
        "is_owner": True, "is_admin": True,
    }


def folder_response(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "channel_folders": items}


def stream_response(items: list[dict[str, object]]) -> dict[str, object]:
    return {"result": "success", "msg": "", "streams": items}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    core.set_configuration_writes_enabled(True)
    request.addfinalizer(lambda: core.set_configuration_writes_enabled(False))
    return client


def test_get_channel_folders_joins_channel_membership(client: Mock) -> None:
    folders = [{
        "id": 4, "name": "Courses", "description": "",
        "is_archived": False, "order": 1,
    }]
    streams = [
        {"stream_id": 12, "name": "rust", "folder_id": 4, "is_archived": False},
        {"stream_id": 13, "name": "general", "folder_id": None},
    ]
    client.call_endpoint.side_effect = [
        folder_response(folders), stream_response(streams),
    ]

    result = mcp_module.get_channel_folders().structured_content

    assert result["status"] == "ok"
    assert result["data"]["channel_folders"][0]["channels"][0]["name"] == "rust"
    assert result["data"]["unassigned_channels"][0]["name"] == "general"


def test_create_channel_folder_dry_run_and_feature_gate(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response([]), stream_response([]),
    ]

    result = mcp_module.create_channel_folder(
        realm_url=REALM_URL, name="Courses", description="Current courses",
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "name": "Courses", "description": "Current courses",
    }

    client.reset_mock()
    client.call_endpoint.side_effect = [server(388), principal()]
    unsupported = mcp_module.create_channel_folder(
        realm_url=REALM_URL, name="Courses", dry_run=True,
    ).structured_content
    assert unsupported["status"] == "unsupported"


def test_create_channel_folder_applies_and_reads_back(client: Mock) -> None:
    created = {
        "id": 4, "name": "Courses", "description": "Current courses",
        "is_archived": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response([]), stream_response([]),
        {"result": "success", "msg": "", "id": 4},
        folder_response([created]), stream_response([]),
    ]

    result = mcp_module.create_channel_folder(
        realm_url=REALM_URL, name="Courses", description="Current courses",
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[4] == call(
        url="/channel_folders/create", method="POST",
        request={"name": "Courses", "description": "Current courses"},
    )


def test_archive_channel_folder_previews_member_channels(client: Mock) -> None:
    folder = {
        "id": 4, "name": "Courses", "description": "Current courses",
        "is_archived": False,
    }
    stream = {
        "stream_id": 12, "name": "rust", "folder_id": 4,
        "is_archived": False,
    }
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response([folder]), stream_response([stream]),
    ]

    result = mcp_module.update_channel_folder(
        realm_url=REALM_URL,
        folder="Courses",
        changes={"is_archived": True},
        expected={"is_archived": False},
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["current"]["channels"][0]["name"] == "rust"
    assert any("does not move" in warning for warning in result["warnings"])


def test_archive_channel_folder_requires_expected_state(client: Mock) -> None:
    client.call_endpoint.side_effect = [server(), principal()]

    result = mcp_module.update_channel_folder(
        realm_url=REALM_URL,
        folder="Courses",
        changes={"is_archived": True},
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EXPECTED_STATE_REQUIRED"
    assert client.call_endpoint.call_count == 2


def test_set_channel_folder_requires_expected_for_move_and_reads_back(
    client: Mock,
) -> None:
    folders = [
        {"id": 4, "name": "Old", "description": "", "is_archived": False},
        {"id": 5, "name": "New", "description": "", "is_archived": False},
    ]
    before = [{
        "stream_id": 12, "name": "rust", "folder_id": 4,
        "is_archived": False,
    }]
    after = [{**before[0], "folder_id": 5}]
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(folders), stream_response(before),
        stream_response(before),
    ]
    conflict = mcp_module.set_channel_folder(
        realm_url=REALM_URL, channel="rust", folder="New", dry_run=True,
    ).structured_content
    assert conflict["status"] == "conflict"
    assert conflict["error"]["code"] == "EXPECTED_STATE_REQUIRED"

    client.reset_mock()
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(folders), stream_response(before),
        stream_response(before), {"result": "success", "msg": ""},
        stream_response(after),
    ]
    result = mcp_module.set_channel_folder(
        realm_url=REALM_URL,
        channel="rust",
        folder="New",
        expected_folder="Old",
    ).structured_content
    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[5] == call(
        url="/streams/12", method="PATCH", request={"folder_id": 5},
    )


def test_set_channel_folder_removal_sends_explicit_json_null(client: Mock) -> None:
    folders = [{
        "id": 4, "name": "Old", "description": "", "is_archived": False,
    }]
    before = [{
        "stream_id": 12, "name": "rust", "folder_id": 4,
        "is_archived": False,
    }]
    after = [{**before[0], "folder_id": None}]
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(folders), stream_response(before),
        stream_response(before), {"result": "success", "msg": ""},
        stream_response(after),
    ]

    result = mcp_module.set_channel_folder(
        realm_url=REALM_URL,
        channel="rust",
        folder=None,
        expected_folder="Old",
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[5] == call(
        url="/streams/12", method="PATCH", request={"folder_id": "null"},
    )


def test_set_channel_folder_removal_requires_explicit_null_readback(
    client: Mock,
) -> None:
    folders = [{
        "id": 4, "name": "Old", "description": "", "is_archived": False,
    }]
    before = [{
        "stream_id": 12, "name": "rust", "folder_id": 4,
        "is_archived": False,
    }]
    incomplete = [{"stream_id": 12, "name": "rust", "is_archived": False}]
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(folders), stream_response(before),
        stream_response(before), {"result": "success", "msg": ""},
        stream_response(incomplete),
    ]

    result = mcp_module.set_channel_folder(
        realm_url=REALM_URL,
        channel="rust",
        folder=None,
        expected_folder="Old",
    ).structured_content

    assert result["status"] == "partial"


def test_set_channel_folder_order_requires_complete_expected_order(client: Mock) -> None:
    folders = [
        {
            "id": 4, "name": "First", "description": "",
            "is_archived": False, "order": 1,
        },
        {
            "id": 5, "name": "Second", "description": "",
            "is_archived": True, "order": 2,
        },
    ]
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(folders), stream_response([]),
    ]

    result = mcp_module.set_channel_folder_order(
        realm_url=REALM_URL,
        folders=["Second", "First"],
        expected_order=["First", "Second"],
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {"order": [5, 4]}


def test_set_channel_folder_order_applies_and_reads_back(client: Mock) -> None:
    before = [
        {
            "id": 4, "name": "First", "description": "",
            "is_archived": False, "order": 1,
        },
        {
            "id": 5, "name": "Second", "description": "",
            "is_archived": False, "order": 2,
        },
    ]
    after = [
        {**before[0], "order": 2},
        {**before[1], "order": 1},
    ]
    client.call_endpoint.side_effect = [
        server(), principal(), folder_response(before), stream_response([]),
        {"result": "success", "msg": ""},
        folder_response(after), stream_response([]),
    ]

    result = mcp_module.set_channel_folder_order(
        realm_url=REALM_URL,
        folders=["Second", "First"],
        expected_order=["First", "Second"],
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[4] == call(
        url="/channel_folders", method="PATCH", request={"order": [5, 4]},
    )
