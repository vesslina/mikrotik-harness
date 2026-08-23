from __future__ import annotations

import asyncio

import pytest

from mth.core.mcp_client import McpTool, McpToolResult
from mth.core.runbooks import (
    APPROVED_TYPED_CHANGE_DOMAINS,
    TypedChangeDefinition,
    typed_definition_for_proposal,
    typed_proposals_for_domains,
)


def _route_tool() -> McpTool:
    return McpTool(
        "manage_route",
        "Add or remove a typed route.",
        {
            "type": "object",
            "properties": {
                "routerId": {"type": "string"},
                "action": {"type": "string", "enum": ["add", "remove"]},
                "dstAddress": {"type": "string"},
                "gateway": {"type": "string"},
                "dryRun": {"type": "boolean"},
            },
            "required": ["routerId", "action", "dstAddress", "gateway"],
            "additionalProperties": False,
        },
        {"readOnlyHint": False, "destructiveHint": True},
    )


def test_live_typed_proposal_preserves_schema_but_owns_safety_arguments() -> None:
    definition = TypedChangeDefinition(_route_tool())
    schema = definition.proposal_schema()

    assert schema["required"] == ["action", "dstAddress", "gateway"]
    assert "routerId" not in schema["properties"]
    assert "dryRun" not in schema["properties"]
    assert definition.proposal_tool.annotations["readOnlyHint"] is True
    assert definition.sanitize_proposal(
        {"action": "add", "dstAddress": "10.0.0.0/24", "gateway": "192.0.2.1"}
    ) == {
        "action": "add",
        "dstAddress": "10.0.0.0/24",
        "gateway": "192.0.2.1",
    }
    with pytest.raises(ValueError, match="Unknown"):
        definition.sanitize_proposal(
            {
                "action": "add",
                "dstAddress": "10.0.0.0/24",
                "gateway": "192.0.2.1",
                "confirmationToken": "bypass",
            }
        )


def test_typed_proposals_are_domain_scoped_and_dangerous_tools_stay_absent() -> None:
    dangerous = McpTool(
        "run_command",
        "Execute a raw command",
        {"type": "object", "properties": {"command": {"type": "string"}}},
        {"readOnlyHint": False, "destructiveHint": True},
    )
    catalog = (_route_tool(), dangerous)

    proposals = typed_proposals_for_domains(catalog, ("firewall_routing",))

    assert [tool.name for tool in proposals] == ["propose_typed_manage_route"]
    assert typed_definition_for_proposal(
        catalog, "propose_typed_manage_route"
    ) is not None
    assert "run_command" not in APPROVED_TYPED_CHANGE_DOMAINS
    assert "reboot" not in APPROVED_TYPED_CHANGE_DOMAINS
    assert "manage_script" not in APPROVED_TYPED_CHANGE_DOMAINS
    assert "manage_container" not in APPROVED_TYPED_CHANGE_DOMAINS


def test_typed_change_uses_dry_run_for_baseline_apply_check_and_rollback_check() -> None:
    class Session:
        def __init__(self) -> None:
            self.states = [
                ("dry_run", {"ip/route": []}),
                ("no_change", {"ip/route": [{"dst-address": "10.0.0.0/24"}]}),
                ("dry_run", {"ip/route": []}),
            ]

        async def call_tool(self, name, arguments=None):
            assert name == "plan_changes"
            assert arguments["steps"][0]["tool"] == "manage_route"
            action, current_state = self.states.pop(0)
            return McpToolResult(
                ("planned",),
                {
                    "steps": [
                        {
                            "currentState": current_state,
                            "structuredDryRun": {"action": action},
                            "dryRunResult": action,
                        }
                    ]
                },
                False,
            )

    async def scenario() -> None:
        definition = TypedChangeDefinition(_route_tool())
        submission = definition.submission(
            {
                "action": "add",
                "dstAddress": "10.0.0.0/24",
                "gateway": "192.0.2.1",
            }
        )
        session = Session()
        baseline = await definition.capture_baseline(
            session, "mikrotik-1", submission.values
        )
        applied = await definition.verify_apply(
            session, "mikrotik-1", submission.values
        )
        rolled_back = await definition.verify_rollback(
            session, "mikrotik-1", submission.values, baseline
        )

        assert applied.passed is True
        assert rolled_back.passed is True
        assert not session.states

    asyncio.run(scenario())


def test_typed_rollback_ignores_routeros_runtime_identity_fields() -> None:
    class Session:
        def __init__(self) -> None:
            self.states = [
                {
                    "items": [
                        {
                            ".id": "*1",
                            "name": "pppoe-wan",
                            "interface": "ether1",
                            "running": False,
                            "status": "disconnected",
                        }
                    ]
                },
                {
                    "items": [
                        {
                            ".id": "*9",
                            "name": "pppoe-wan",
                            "interface": "ether1",
                            "running": True,
                            "status": "connected",
                        }
                    ]
                },
            ]

        async def call_tool(self, name, arguments=None):
            assert name == "plan_changes"
            current_state = self.states.pop(0)
            return McpToolResult(
                ("planned",),
                {
                    "steps": [
                        {
                            "currentState": current_state,
                            "structuredDryRun": {"action": "no_change"},
                        }
                    ]
                },
                False,
            )

    async def scenario() -> None:
        definition = TypedChangeDefinition(_route_tool())
        submission = definition.submission(
            {
                "action": "remove",
                "dstAddress": "10.0.0.0/24",
                "gateway": "192.0.2.1",
            }
        )
        session = Session()
        baseline = await definition.capture_baseline(
            session, "mikrotik-1", submission.values
        )
        result = await definition.verify_rollback(
            session, "mikrotik-1", submission.values, baseline
        )
        assert result.passed is True

    asyncio.run(scenario())


def test_typed_remove_treats_not_found_post_check_as_verified_absence() -> None:
    class Session:
        async def call_tool(self, name, arguments=None):
            return McpToolResult(
                ("planned",),
                {
                    "steps": [
                        {
                            "currentState": [],
                            "structuredDryRun": {
                                "action": "would_fail",
                                "error": "IP_ADDRESS_NOT_FOUND",
                            },
                            "dryRunResult": "IP address not found",
                        }
                    ]
                },
                False,
            )

    async def scenario() -> None:
        definition = TypedChangeDefinition(_route_tool())
        submission = definition.submission(
            {
                "action": "remove",
                "dstAddress": "10.0.0.0/24",
                "gateway": "192.0.2.1",
            }
        )
        result = await definition.verify_apply(Session(), "router", submission.values)
        assert result.passed is True
        assert "absent" in result.details

    asyncio.run(scenario())
