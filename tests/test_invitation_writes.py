import importlib
from unittest.mock import Mock, call

import pytest

from zulipmcp import core
from zulipmcp.configuration import APIError, REDACTED, ZulipAPIError

mcp_module = importlib.import_module("zulipmcp.mcp")
REALM_URL = "https://realm.example.test"


def email_invite(
    email: str = "new@example.test",
    *,
    invite_id: int = 7,
    invited_at: int = 1000,
) -> dict[str, object]:
    return {
        "id": invite_id,
        "email": email,
        "is_multiuse": False,
        "invited": invited_at,
        "expiry_date": invited_at + 14400 * 60,
        "invited_as": 400,
        "invited_by_user_id": 1,
    }


def reusable_invite() -> dict[str, object]:
    return {
        "id": 8,
        "is_multiuse": True,
        "invited": 1100,
        "expiry_date": 2100,
        "invited_as": 400,
        "invited_by_user_id": 1,
        "link_url": "https://realm.example.test/join/private-token/",
    }


@pytest.fixture(autouse=True)
def setup(monkeypatch: pytest.MonkeyPatch) -> None:
    core.set_configuration_writes_enabled(True)
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL},
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    yield
    core.set_configuration_writes_enabled(False)


def invitation_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        core,
        "get_streams_configuration",
        lambda: {"streams": [{"stream_id": 10, "name": "general"}]},
    )
    monkeypatch.setattr(
        core,
        "_group_inventory",
        lambda: [{"id": 20, "name": "students", "deactivated": False}],
    )
    monkeypatch.setattr(
        core,
        "get_users_configuration",
        lambda: {"members": [{"email": "owner@example.test"}]},
    )


def test_invite_users_dry_run_resolves_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    monkeypatch.setattr(
        core, "get_invitations_configuration", lambda: {"invites": []},
    )
    mutation = Mock()
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.invite_users(
        REALM_URL,
        [" NEW@example.test "],
        "member",
        ["general"],
        groups=["students"],
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"] == {
        "invitee_emails": "new@example.test",
        "invite_as": 400,
        "stream_ids": [10],
        "group_ids": [20],
        "include_realm_default_subscriptions": True,
        "invite_expires_in_minutes": 14400,
        "notify_referrer_on_join": False,
    }
    assert "externally visible" in result["warnings"][0]
    mutation.assert_not_called()


def test_invite_users_encodes_never_expiring_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    monkeypatch.setattr(
        core, "get_invitations_configuration", lambda: {"invites": []},
    )

    result = mcp_module.invite_users(
        REALM_URL,
        ["new@example.test"],
        "member",
        [],
        expires_in_minutes=None,
        dry_run=True,
    ).structured_content

    assert result["status"] == "dry_run"
    assert result["request"]["invite_expires_in_minutes"] == "null"


def test_invite_users_rejects_existing_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    monkeypatch.setattr(
        core,
        "get_users_configuration",
        lambda: {"members": [{"email": "new@example.test"}]},
    )

    result = mcp_module.invite_users(
        REALM_URL, ["new@example.test"], "member", [], dry_run=True,
    ).structured_content

    assert result["status"] == "conflict"
    assert result["error"]["code"] == "USER_ALREADY_EXISTS"


@pytest.mark.parametrize(
    "email",
    [
        "Alias <new@example.test>",
        "<new@example.test>",
        "new@example.test(comment)",
    ],
)
def test_invite_users_rejects_decorated_email_forms(
    monkeypatch: pytest.MonkeyPatch, email: str,
) -> None:
    invitation_inputs(monkeypatch)

    result = mcp_module.invite_users(
        REALM_URL, [email], "member", [], dry_run=True,
    ).structured_content

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_EMAIL"


def test_invite_users_reports_server_partial_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    inventories = Mock(side_effect=[
        {"invites": []},
        {"invites": [email_invite()]},
    ])
    monkeypatch.setattr(core, "get_invitations_configuration", inventories)
    mutation = Mock(side_effect=ZulipAPIError(
        APIError("Some invitations failed", "INVITATION_FAILED", 400),
        {"sent_invitations": True, "errors": [["other@example.test", "bad", False]]},
    ))
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.invite_users(
        REALM_URL,
        ["new@example.test", "other@example.test"],
        "member",
        [],
    ).structured_content

    assert result["status"] == "partial"
    assert result["completed_fields"] == ["new@example.test"]
    assert result["remaining_fields"] == ["other@example.test"]
    assert result["error"]["code"] == "INVITATION_FAILED"


def test_invite_users_reports_ignored_options_as_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": []}, {"invites": [email_invite()]}]),
    )
    monkeypatch.setattr(
        core,
        "configuration_mutation_request",
        Mock(return_value={
            "ignored_parameters_unsupported": ["notify_referrer_on_join"],
        }),
    )

    result = mcp_module.invite_users(
        REALM_URL, ["new@example.test"], "member", [],
    ).structured_content

    assert result["status"] == "partial"
    assert result["unsupported_fields"] == ["notify_referrer_on_join"]


def test_invite_users_allows_small_expiry_clock_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    invite = email_invite()
    invite["expiry_date"] = int(invite["expiry_date"]) + 1
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": []}, {"invites": [invite]}]),
    )
    monkeypatch.setattr(
        core, "configuration_mutation_request", Mock(return_value={}),
    )

    result = mcp_module.invite_users(
        REALM_URL, ["new@example.test"], "member", [],
    ).structured_content

    assert result["status"] == "ok"


def test_invite_users_rejects_missing_finite_expiry_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invitation_inputs(monkeypatch)
    invite = email_invite()
    invite["expiry_date"] = None
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": []}, {"invites": [invite]}]),
    )
    monkeypatch.setattr(
        core, "configuration_mutation_request", Mock(return_value={}),
    )

    result = mcp_module.invite_users(
        REALM_URL, ["new@example.test"], "member", [],
    ).structured_content

    assert result["status"] == "partial"
    assert result["remaining_fields"] == ["new@example.test"]


def test_invite_users_preflights_feature_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "_admin_destination",
        lambda endpoint, realm_url, dry_run: (
            {"realm_url": REALM_URL, "zulip_feature_level": 260},
            {"user_id": 1, "is_owner": True, "is_admin": True},
            None,
        ),
    )
    invitation_inputs(monkeypatch)
    monkeypatch.setattr(
        core, "get_invitations_configuration", lambda: {"invites": []},
    )

    result = mcp_module.invite_users(
        REALM_URL, ["new@example.test"], "member", [], dry_run=True,
    ).structured_content

    assert result["status"] == "unsupported"
    assert result["unsupported_fields"] == [
        "include_realm_default_subscriptions",
        "notify_referrer_on_join",
    ]


def test_resend_email_invitation_is_explicit_external_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = email_invite()
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": [invite]}, {"invites": [invite]}]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.resend_email_invitation(
        REALM_URL, "new@example.test", expected_invited_at=1000,
    ).structured_content

    assert result["status"] == "ok"
    assert "externally visible" in result["warnings"][0]
    mutation.assert_called_once_with("/invites/7/resend", "POST", {})


def test_revoke_email_invitation_reads_back_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": [email_invite()]}, {"invites": []}]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.revoke_email_invitation(
        REALM_URL, "new@example.test", expected_invited_at=1000,
    ).structured_content

    assert result["status"] == "ok"
    assert result["readback"] == {"present": False}
    mutation.assert_called_once_with("/invites/7", "DELETE", {})


def test_revoke_reusable_invitation_resolves_timestamp_without_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        core,
        "get_invitations_configuration",
        Mock(side_effect=[{"invites": [reusable_invite()]}, {"invites": []}]),
    )
    mutation = Mock(return_value={})
    monkeypatch.setattr(core, "configuration_mutation_request", mutation)

    result = mcp_module.revoke_reusable_invitation(
        REALM_URL, 1100,
    ).structured_content

    assert result["status"] == "ok"
    assert "private-token" not in str(result)
    mutation.assert_called_once_with("/invites/multiuse/8", "DELETE", {})


def test_invitation_audit_redacts_reusable_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock(api_key="private-api-key")
    client.call_endpoint.return_value = {
        "result": "success",
        "msg": "",
        "invites": [reusable_invite()],
    }
    monkeypatch.setattr(core, "get_client", lambda: client)

    result = mcp_module.get_invitations().structured_content

    assert result["data"]["invites"][0]["link_url"] == REDACTED
    assert "private-token" not in str(result)
    assert client.call_endpoint.call_args_list == [
        call(url="/invites", method="GET", request=None),
    ]


def test_error_response_scrubs_unlabelled_client_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock(api_key="private-api-key")
    client.call_endpoint.return_value = {
        "result": "error",
        "msg": "Invitation failed",
        "code": "INVITATION_FAILED",
        "detail": "Unexpected echo private-api-key",
        "sent_invitations": True,
    }
    monkeypatch.setattr(core, "get_client", lambda: client)

    with pytest.raises(ZulipAPIError) as caught:
        core.configuration_request("/invites", "POST", {})

    assert caught.value.response is not None
    assert caught.value.response["detail"] == f"Unexpected echo {REDACTED}"
    assert "private-api-key" not in str(caught.value.response)
