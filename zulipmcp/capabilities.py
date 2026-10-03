from __future__ import annotations

from typing import Any

from .configuration import JSONValue, ORGANIZATION_SECTIONS


AUDIT_TOOLS = (
    "get_administration_capabilities",
    "get_allowed_domains",
    "get_bots",
    "get_channel_folders",
    "get_current_user",
    "get_custom_emoji",
    "get_custom_profile_fields",
    "get_data_exports",
    "get_invitations",
    "get_linkifiers",
    "get_moderation_configuration",
    "get_organization_branding",
    "get_organization_configuration",
    "get_server_settings",
    "get_stream_members",
    "get_user_groups",
    "get_user_info",
    "get_users",
    "list_streams",
)

AUDIT_SECTIONS = (
    "allowed_domains",
    "bots",
    "channel_folders",
    "channels",
    "custom_emoji",
    "custom_profile_fields",
    "data_exports",
    "invitations",
    "linkifiers",
    "moderation",
    "organization_branding",
    "organization_configuration",
    "principal",
    "server",
    "user_groups",
    "users",
)


CAPABILITY_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "organization.audit",
        "audit_tools": AUDIT_TOOLS,
        "mutation_tools": (),
        "authority": "varies_by_section",
        "reversibility": "not_applicable",
    },
    {
        "id": "organization.settings",
        "audit_tools": ("get_organization_configuration",),
        "mutation_tools": (
            "update_organization_configuration",
            "update_default_user_settings",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible",
    },
    {
        "id": "organization.authentication",
        "audit_tools": ("get_organization_configuration",),
        "mutation_tools": ("update_organization_configuration",),
        "authority": "organization_owner",
        "gate": "configuration",
        "reversibility": "reversible",
        "requirements": (
            "exact expected authentication-method map",
            "at least one supported method remains enabled",
        ),
        "known_gaps": (
            "External identity-provider health cannot be proved by preflight",
        ),
    },
    {
        "id": "users.lifecycle",
        "audit_tools": ("get_users", "get_user_info"),
        "mutation_tools": (
            "invite_users",
            "resend_email_invitation",
            "revoke_email_invitation",
            "revoke_reusable_invitation",
            "set_user_active",
            "update_user_configuration",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "mixed",
        "side_effects": (
            "Invitation actions can send email",
            "User deactivation also deactivates owned bots",
        ),
    },
    {
        "id": "invitations.default_channels",
        "audit_tools": ("get_invitations", "list_streams"),
        "mutation_tools": ("invite_users",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 261,
        "reversibility": "reversible_by_revocation",
        "requirements": ("include_default_channels option",),
    },
    {
        "id": "invitations.suppress_referrer_notification",
        "audit_tools": ("get_invitations",),
        "mutation_tools": ("invite_users",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 267,
        "reversibility": "reversible_by_revocation",
        "requirements": ("notify_referrer_on_join=false option",),
    },
    {
        "id": "invitations.group_assignment",
        "audit_tools": ("get_invitations", "get_user_groups"),
        "mutation_tools": ("invite_users",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 322,
        "reversibility": "reversible_by_revocation",
        "requirements": ("groups option is nonempty",),
    },
    {
        "id": "channels.manage",
        "audit_tools": ("list_streams", "get_stream_members"),
        "mutation_tools": (
            "set_channel_members",
            "subscribe_users_to_channel",
            "unsubscribe_users_from_channel",
            "update_channel_configuration",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible",
    },
    {
        "id": "channels.create",
        "audit_tools": ("list_streams",),
        "mutation_tools": ("create_channel",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 417,
        "reversibility": "reversible_by_archival",
    },
    {
        "id": "channels.archive",
        "audit_tools": ("list_streams",),
        "mutation_tools": ("archive_channel", "set_channel_archived"),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 315,
        "reversibility": "reversible_from_feature_level_388",
        "requirements": ("set_channel_archived archived=true",),
    },
    {
        "id": "channels.unarchive",
        "audit_tools": ("list_streams",),
        "mutation_tools": ("set_channel_archived",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 388,
        "reversibility": "reversible",
        "requirements": ("set_channel_archived archived=false",),
    },
    {
        "id": "channels.defaults",
        "audit_tools": ("list_streams",),
        "mutation_tools": ("set_default_channel", "set_default_channels"),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 200,
        "reversibility": "reversible",
    },
    {
        "id": "channels.folders",
        "audit_tools": ("get_channel_folders",),
        "mutation_tools": (
            "create_channel_folder",
            "set_channel_folder",
            "update_channel_folder",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 389,
        "reversibility": "reversible",
    },
    {
        "id": "channels.folder_order",
        "audit_tools": ("get_channel_folders",),
        "mutation_tools": ("set_channel_folder_order",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 414,
        "reversibility": "reversible",
    },
    {
        "id": "groups.manage",
        "audit_tools": ("get_user_groups",),
        "mutation_tools": (
            "create_user_group",
            "set_user_group_members",
            "update_user_group",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible",
    },
    {
        "id": "groups.deactivate",
        "audit_tools": ("get_user_groups",),
        "mutation_tools": ("set_user_group_active",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 290,
        "reversibility": "reversible",
        "requirements": ("set_user_group_active active=false",),
    },
    {
        "id": "groups.reactivate",
        "audit_tools": ("get_user_groups",),
        "mutation_tools": ("set_user_group_active",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 386,
        "reversibility": "reversible",
        "requirements": ("set_user_group_active active=true",),
    },
    {
        "id": "profile_fields.lifecycle",
        "audit_tools": ("get_custom_profile_fields",),
        "mutation_tools": (
            "create_custom_profile_field",
            "delete_custom_profile_field",
            "update_custom_profile_field",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "mixed",
        "side_effects": ("Deletion permanently removes field values",),
    },
    {
        "id": "profile_fields.user_matching",
        "audit_tools": ("get_custom_profile_fields",),
        "mutation_tools": (
            "create_custom_profile_field",
            "update_custom_profile_field",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 455,
        "reversibility": "reversible",
        "requirements": ("use_for_user_matching field",),
    },
    {
        "id": "domains.lifecycle",
        "audit_tools": ("get_allowed_domains",),
        "mutation_tools": (
            "add_allowed_domain",
            "remove_allowed_domain",
            "update_allowed_domain",
        ),
        "authority": "organization_owner",
        "gate": "configuration",
        "reversibility": "reversible",
    },
    {
        "id": "linkifiers.lifecycle",
        "audit_tools": ("get_linkifiers",),
        "mutation_tools": (
            "create_linkifier",
            "remove_linkifier",
            "update_linkifier",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible",
    },
    {
        "id": "linkifiers.reverse_matching",
        "audit_tools": ("get_linkifiers",),
        "mutation_tools": ("create_linkifier", "update_linkifier"),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 471,
        "reversibility": "reversible",
        "requirements": ("reverse-linkifier fields",),
    },
    {
        "id": "emoji.lifecycle",
        "audit_tools": ("get_custom_emoji",),
        "mutation_tools": (
            "deactivate_custom_emoji",
            "upload_custom_emoji",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "partially_reversible",
        "known_gaps": ("Custom emoji reactivation has no documented API",),
    },
    {
        "id": "branding.assets",
        "audit_tools": (
            "download_organization_branding",
            "get_organization_branding",
        ),
        "mutation_tools": ("upload_organization_branding",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible_with_saved_asset",
        "known_gaps": (
            "Resetting branding to server defaults has no documented API",
        ),
    },
    {
        "id": "bots.lifecycle",
        "audit_tools": ("get_bots",),
        "mutation_tools": (
            "create_bot",
            "set_bot_active",
            "set_bot_channel_subscriptions",
            "update_bot_configuration",
        ),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "reversible",
        "known_gaps": (
            "Bot API-key retrieval and regeneration are intentionally not exposed",
        ),
    },
    {
        "id": "moderation.report_message",
        "audit_tools": ("get_moderation_configuration",),
        "mutation_tools": ("report_message",),
        "authority": "authorized_user",
        "gate": "user_content",
        "minimum_feature_level": 382,
        "required_server_field": "realm_moderation_request_channel_id",
        "required_server_field_test": "positive_integer",
        "reversibility": "irreversible_notification",
        "confirmation": "exact_message_phrase",
        "side_effects": ("Sends a report to the moderation channel",),
    },
    {
        "id": "moderation.delete_message",
        "audit_tools": ("get_moderation_configuration",),
        "mutation_tools": ("delete_message_for_moderation",),
        "authority": "organization_administrator",
        "gate": "user_content",
        "reversibility": "irreversible",
        "confirmation": "exact_message_phrase",
    },
    {
        "id": "exports.public",
        "audit_tools": ("get_data_exports",),
        "mutation_tools": ("create_data_export", "delete_data_export"),
        "authority": "organization_administrator",
        "gate": "configuration",
        "reversibility": "mixed",
        "confirmation": "exact_export_phrase",
        "side_effects": (
            "Creation is asynchronous and may send an administrator notification",
            "Deletion permanently removes the hosted archive",
        ),
        "known_gaps": ("Bearer download URLs are always redacted",),
    },
    {
        "id": "exports.full_with_consent",
        "audit_tools": ("get_data_exports",),
        "mutation_tools": ("create_data_export",),
        "authority": "organization_administrator",
        "gate": "configuration",
        "minimum_feature_level": 304,
        "reversibility": "irreversible_notification",
        "confirmation": "exact_export_phrase",
    },
    {
        "id": "exports.full_without_consent",
        "audit_tools": ("get_data_exports",),
        "mutation_tools": ("create_data_export",),
        "authority": "organization_owner",
        "gate": "configuration",
        "minimum_feature_level": 449,
        "required_server_field": "realm_owner_full_content_access",
        "reversibility": "irreversible_notification",
        "confirmation": "exact_export_phrase",
    },
    {
        "id": "invitations.reusable_create",
        "implementation_status": "not_implemented",
        "audit_tools": ("get_invitations",),
        "mutation_tools": (),
        "authority": "authorized_user",
        "reversibility": "reversible",
        "known_gaps": (
            "Secure bearer-link delivery is not available through normal MCP results",
        ),
    },
    {
        "id": "moderation.report_workflow",
        "implementation_status": "unsupported",
        "audit_tools": ("get_moderation_configuration",),
        "mutation_tools": (),
        "authority": "organization_moderator",
        "reversibility": "not_applicable",
        "known_gaps": (
            "Zulip exposes no dedicated report queue or resolution API",
        ),
    },
    {
        "id": "organization.deactivate",
        "implementation_status": "not_implemented",
        "audit_tools": ("get_organization_configuration",),
        "mutation_tools": (),
        "authority": "organization_owner",
        "minimum_feature_level": 332,
        "reversibility": "potentially_irreversible",
        "known_gaps": (
            "Catastrophic realm deactivation is intentionally not exposed by an ordinary tool",
        ),
    },
    {
        "id": "organization.reactivate",
        "implementation_status": "unsupported",
        "audit_tools": (),
        "mutation_tools": (),
        "authority": "server_administrator",
        "reversibility": "not_applicable",
        "known_gaps": ("Reactivation requires a server management command",),
    },
    {
        "id": "organization.delete",
        "implementation_status": "unsupported",
        "audit_tools": (),
        "mutation_tools": (),
        "authority": "server_administrator",
        "reversibility": "irreversible",
        "known_gaps": (
            "Zulip exposes no standalone organization-deletion REST API",
        ),
    },
)


def _authority_status(
    authority: str, principal: dict[str, JSONValue] | None,
) -> str:
    if authority in {"server_administrator", "server_authoritative"}:
        return "server_authoritative"
    if authority in {"varies_by_section", "organization_moderator"}:
        return "unknown"
    if principal is None:
        return "unknown"
    if authority == "organization_owner":
        return "available" if principal.get("is_owner") is True else "insufficient"
    if authority == "organization_administrator":
        allowed = principal.get("is_owner") is True or principal.get("is_admin") is True
        return "available" if allowed else "insufficient"
    if authority == "authorized_user":
        return "available" if principal.get("is_active") is True else "insufficient"
    return "unknown"


def evaluate_administration_capabilities(
    server: dict[str, JSONValue] | None,
    principal: dict[str, JSONValue] | None,
    realm_fields: dict[str, JSONValue] | None = None,
) -> dict[str, JSONValue]:
    feature_level = server.get("zulip_feature_level") if server else None
    realm_fields = realm_fields or {}
    capabilities: list[JSONValue] = []
    for definition in CAPABILITY_DEFINITIONS:
        item: dict[str, JSONValue] = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in definition.items()
        }
        implementation = str(item.get("implementation_status", "supported"))
        minimum = item.get("minimum_feature_level", 0)
        if implementation == "unsupported":
            server_support = "unsupported"
        elif not isinstance(feature_level, int) or isinstance(feature_level, bool):
            server_support = "unknown"
        elif isinstance(minimum, int) and feature_level < minimum:
            server_support = "unsupported"
        else:
            server_support = "supported"
        support = (
            implementation
            if implementation != "supported"
            else server_support
        )
        authority = str(item.get("authority", "unknown"))
        authority_status = _authority_status(authority, principal)
        required_field = item.get("required_server_field")
        requirement_status: JSONValue = None
        if isinstance(required_field, str) and support == "supported":
            requirement_status = realm_fields.get(required_field)
            requirement_test = item.get("required_server_field_test", "true")
            if requirement_test == "positive_integer":
                requirement_met = (
                    isinstance(requirement_status, int)
                    and not isinstance(requirement_status, bool)
                    and requirement_status > 0
                )
            else:
                requirement_met = requirement_status is True
            if not requirement_met:
                support = (
                    "unknown" if requirement_status is None else "conditional"
                )
        if support == "supported" and authority_status in {"insufficient", "unknown"}:
            support = "conditional"
        item.update({
            "implementation_status": implementation,
            "support": support,
            "server_support": server_support,
            "authority_status": authority_status,
            "required_feature_level": minimum,
        })
        if isinstance(required_field, str):
            item["required_server_field_status"] = requirement_status
        capabilities.append(item)
    return {
        "zulip_version": server.get("zulip_version") if server else None,
        "zulip_feature_level": feature_level,
        "principal": principal,
        "supported_audit_sections": list(AUDIT_SECTIONS),
        "organization_snapshot_sections": list(ORGANIZATION_SECTIONS),
        "audit_tools": list(AUDIT_TOOLS),
        "capabilities": capabilities,
    }
