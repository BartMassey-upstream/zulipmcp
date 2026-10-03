import importlib
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from zulipmcp import core

mcp_module = importlib.import_module("zulipmcp.mcp")

REALM_URL = "https://realm.example.test"
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic image data"


def register_response(source: str = "D") -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "queue_id": "queue-1",
        "realm_icon_url": "/user_avatars/1/realm/icon.png",
        "realm_icon_source": source,
        "max_icon_file_size_mib": 5,
        "realm_logo_url": "/static/logo.png",
        "realm_logo_source": "D",
        "realm_night_logo_url": "/static/logo-night.png",
        "realm_night_logo_source": "D",
        "max_logo_file_size_mib": 5,
    }


def server() -> dict[str, object]:
    return {"result": "success", "msg": "", "realm_url": REALM_URL}


def principal() -> dict[str, object]:
    return {
        "result": "success",
        "msg": "",
        "is_owner": True,
        "is_admin": True,
    }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Mock:
    client = Mock(api_key="private-key", base_url=f"{REALM_URL}/api")
    monkeypatch.setattr(core, "get_client", lambda: client)
    core.set_configuration_writes_enabled(True)
    request.addfinalizer(lambda: core.set_configuration_writes_enabled(False))
    return client


def test_get_branding_projects_all_assets(client: Mock) -> None:
    client.call_endpoint.side_effect = [
        register_response(), {"result": "success", "msg": ""},
    ]

    result = mcp_module.get_organization_branding().structured_content

    assert result["status"] == "ok"
    assert result["data"]["icon"] == {
        "asset": "icon",
        "url": "/user_avatars/1/realm/icon.png",
        "source": "D",
        "max_file_size_mib": 5,
    }
    assert result["data"]["logo_dark"]["url"] == "/static/logo-night.png"


def test_branding_reports_missing_required_fields_as_partial(client: Mock) -> None:
    response = register_response()
    del response["realm_night_logo_url"]
    client.call_endpoint.side_effect = [response, {"result": "success", "msg": ""}]

    result = mcp_module.get_organization_branding().structured_content

    assert result["status"] == "partial"
    assert "url" not in result["data"]["logo_dark"]
    assert result["absent_fields"] == ["realm_night_logo_url"]


def test_download_branding_uses_authenticated_session_and_private_file(
    client: Mock, tmp_path: Path,
) -> None:
    response = Mock(
        status_code=200,
        content=PNG,
        headers={"Content-Type": "image/png"},
    )
    client.session = Mock()
    client.session.get.return_value = response
    client.call_endpoint.side_effect = [
        register_response(), {"result": "success", "msg": ""},
    ]

    result = mcp_module.download_organization_branding(
        "icon", str(tmp_path),
    ).structured_content

    assert result["status"] == "ok"
    data = result["data"]
    saved = Path(data["path"])
    assert saved.read_bytes() == PNG
    assert saved.stat().st_mode & 0o777 == 0o600
    assert data["media_type"] == "image/png"
    assert data["byte_count"] == len(PNG)
    assert len(data["sha256"]) == 64
    client.session.get.assert_called_once_with(
        f"{REALM_URL}/user_avatars/1/realm/icon.png", timeout=30,
    )


def test_upload_branding_dry_run_validates_without_post(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "untrusted-extension.txt"
    image.write_bytes(PNG)
    client.call_endpoint.side_effect = [
        server(), principal(), register_response(), {"result": "success", "msg": ""},
    ]

    result = mcp_module.upload_organization_branding(
        REALM_URL, "logo_dark", str(image), expected_source="D", dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"]["night"] is True
    assert result["request"]["file"]["media_type"] == "image/png"
    assert "bytes" not in result["request"]["file"]
    assert all(item.kwargs["method"] != "POST" or item.kwargs["url"] == "/register"
               for item in client.call_endpoint.call_args_list)


def test_upload_branding_posts_multipart_and_confirms_readback(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "icon.png"
    image.write_bytes(PNG)
    client.call_endpoint.side_effect = [
        server(),
        principal(),
        register_response(),
        {"result": "success", "msg": ""},
        {"result": "success", "msg": "", "icon_url": "/uploaded/icon.png"},
        register_response("U"),
        {"result": "success", "msg": ""},
    ]

    result = mcp_module.upload_organization_branding(
        REALM_URL, "icon", str(image), expected_source="D",
    ).structured_content

    assert result["status"] == "ok"
    upload = client.call_endpoint.call_args_list[4]
    assert upload.kwargs["url"] == "/realm/icon"
    assert upload.kwargs["method"] == "POST"
    assert upload.kwargs["request"] == {}
    assert len(upload.kwargs["files"]) == 1
    assert result["readback"]["source"] == "U"


def test_upload_branding_rejects_active_or_invalid_image(
    client: Mock, tmp_path: Path,
) -> None:
    image = tmp_path / "image.svg"
    image.write_text("<svg><script>alert(1)</script></svg>")
    client.call_endpoint.side_effect = [
        server(), principal(), register_response(), {"result": "success", "msg": ""},
    ]

    result = mcp_module.upload_organization_branding(
        REALM_URL, "icon", str(image), dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_IMAGE"
    assert call(url="/realm/icon", method="POST", request={}) \
        not in client.call_endpoint.call_args_list
