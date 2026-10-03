import importlib
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")

REALM_URL = "https://realm.example.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic image data"


def server() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "realm_url": REALM_URL,
        "zulip_feature_level": 500,
    }


def principal() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "is_owner": True,
        "is_admin": True,
    }


def emoji(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "emoji": {str(item["id"]): item for item in items},
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Mock:
    client = Mock(api_key="private-key")
    monkeypatch.setattr(core, "get_client", lambda: client)
    core.set_configuration_writes_enabled(True)
    request.addfinalizer(lambda: core.set_configuration_writes_enabled(False))
    return client


def test_upload_custom_emoji_dry_run_validates_file(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "emoji.data"
    image.write_bytes(PNG)
    client.call_endpoint.side_effect = [server(), principal(), emoji([])]

    result = mcp_module.upload_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="Party Parrot",
        file_path=str(image),
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["endpoint"] == "/realm/emoji/party_parrot"
    assert result["request"]["file"]["media_type"] == "image/png"
    assert result["request"]["file"]["byte_count"] == len(PNG)
    assert client.call_endpoint.call_count == 3


def test_upload_custom_emoji_posts_and_reads_back(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "emoji.png"
    image.write_bytes(PNG)
    created = {"id": "4", "name": "party_parrot", "deactivated": False}
    client.call_endpoint.side_effect = [
        server(), principal(), emoji([]),
        {"result": "success", "msg": ""}, emoji([created]),
    ]

    result = mcp_module.upload_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="Party_Parrot",
        file_path=str(image),
    ).structured_content

    assert result["status"] == "ok"
    upload = client.call_endpoint.call_args_list[3]
    assert upload.kwargs["url"] == "/realm/emoji/party_parrot"
    assert upload.kwargs["method"] == "POST"
    assert upload.kwargs["request"] == {}
    assert len(upload.kwargs["files"]) == 1


def test_upload_custom_emoji_sends_the_validated_bytes(
    client: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = tmp_path / "emoji.png"
    image.write_bytes(PNG)
    created = {"id": "4", "name": "party_parrot", "deactivated": False}
    client.call_endpoint.side_effect = [
        server(), principal(), emoji([]), emoji([created]),
    ]
    original_reader = core._read_image_file

    def replace_after_read(*args: object) -> tuple[Path, bytes, str, str]:
        result = original_reader(*args)
        image.write_bytes(b"not an image and not the validated content")
        return result

    uploaded = b""

    def capture_upload(
        endpoint: str, method: str, request: dict[str, object], files: list[object],
    ) -> dict[str, object]:
        nonlocal uploaded
        uploaded = files[0].read()
        return {"result": "success", "msg": ""}

    monkeypatch.setattr(core, "_read_image_file", replace_after_read)
    monkeypatch.setattr(core, "configuration_mutation_request", capture_upload)

    result = mcp_module.upload_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="Party Parrot",
        file_path=str(image),
    ).structured_content

    assert result["status"] == "ok"
    assert uploaded == PNG


def test_upload_custom_emoji_rejects_collision_before_file_read(
    client: Mock, tmp_path: Path,
) -> None:
    existing = {"id": "4", "name": "party parrot", "deactivated": True}
    client.call_endpoint.side_effect = [server(), principal(), emoji([existing])]

    result = mcp_module.upload_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="Party_Parrot",
        file_path=str(tmp_path / "missing.png"),
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "EMOJI_ALREADY_EXISTS"


def test_upload_custom_emoji_rejects_symlink(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(PNG)
    link = tmp_path / "link.png"
    link.symlink_to(image)
    client.call_endpoint.side_effect = [server(), principal(), emoji([])]

    result = mcp_module.upload_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="safe",
        file_path=str(link),
        dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "LOCAL_FILE_INVALID"


def test_deactivate_custom_emoji_checks_expected_and_reads_back(client: Mock) -> None:
    active = {"id": "4", "name": "party_parrot", "deactivated": False}
    inactive = {**active, "deactivated": True}
    client.call_endpoint.side_effect = [
        server(), principal(), emoji([active]),
        {"result": "success", "msg": ""}, emoji([inactive]),
    ]

    result = mcp_module.deactivate_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="Party Parrot",
        expected_deactivated=False,
    ).structured_content

    assert result["status"] == "ok"
    assert client.call_endpoint.call_args_list[3] == call(
        url="/realm/emoji/party_parrot", method="DELETE", request={},
    )
    assert any("historical" in warning for warning in result["warnings"])


def test_deactivate_custom_emoji_rejects_stale_state(client: Mock) -> None:
    active = {"id": "4", "name": "party_parrot", "deactivated": False}
    client.call_endpoint.side_effect = [server(), principal(), emoji([active])]

    result = mcp_module.deactivate_custom_emoji(
        realm_url=REALM_URL,
        emoji_name="party_parrot",
        expected_deactivated=True,
        dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert client.call_endpoint.call_count == 3
