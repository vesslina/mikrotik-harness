from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from mth.core.mcp_client import McpTool
from mth.core.runbooks.base import (
    RunbookDefinition,
    RunbookStep,
    RunbookSubmission,
    RunbookVerification,
    ToolSession,
)

_HARNESS_ARGUMENTS = frozenset({"routerId", "dryRun", "confirmationToken"})
_SETTLED_ACTIONS = frozenset(
    {
        "already_exists",
        "already_removed",
        "already_set",
        "already_static",
        "already_trusted",
        "already_untrusted",
        "no_change",
        "not_found",
    }
)

# RouterOS assigns a fresh internal ``.id`` when a record is recreated and
# includes several live counters/status fields in list snapshots. Those values
# are useful for diagnostics, but they are not configuration identity. Keeping
# them in a rollback fingerprint makes a successful restore look like a failure.
_VOLATILE_STATE_KEYS = frozenset(
    {
        ".id",
        ".about",
        ".nextid",
        "about",
        "running",
        "status",
        "uptime",
        "bytes",
        "packets",
        "dynamic",
        "invalid",
        "dead",
        "expired",
        "active",
        "creation-time",
        "last-logged-in",
        "last-link-up-time",
        "last-link-down-time",
        "link-downs",
        "last-link-down",
        "last-link-up",
        "rx-byte",
        "tx-byte",
        "rx-packet",
        "tx-packet",
    }
)


def _canonical_state(value: Any) -> Any:
    """Return a stable, configuration-oriented RouterOS snapshot."""

    if isinstance(value, Mapping):
        dynamic = value.get("dynamic")
        if dynamic is True or (
            isinstance(dynamic, str) and dynamic.casefold() in {"true", "yes"}
        ):
            return None
        return {
            str(key): _canonical_state(item)
            for key, item in value.items()
            if str(key).casefold() not in _VOLATILE_STATE_KEYS
        }
    if isinstance(value, list):
        # Preserve list order: it is meaningful for firewall and routing rules.
        return [
            normalized
            for item in value
            if (normalized := _canonical_state(item)) is not None
        ]
    if isinstance(value, tuple):
        return [_canonical_state(item) for item in value]
    return value


def _state_fingerprint(value: Any) -> str:
    canonical = _canonical_state(value)
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()

# Scenario runbooks still provide richer forms and specialised verification.  The generic
# proposal path intentionally follows the live catalog, however: READY must not hide a
# RouterOS configuration capability merely because a bespoke runbook has not been written.
APPROVED_TYPED_CHANGE_DOMAINS: dict[str, frozenset[str]] = {
    "manage_address_list_entry": frozenset({"firewall_routing"}),
    "manage_bridge": frozenset({"interfaces"}),
    "manage_bridge_port": frozenset({"interfaces"}),
    "manage_dhcp_client": frozenset({"addressing_services", "interfaces"}),
    "manage_dhcp_lease": frozenset({"addressing_services"}),
    "manage_dhcp_server": frozenset({"addressing_services"}),
    "manage_dns_entry": frozenset({"addressing_services"}),
    "manage_dns_settings": frozenset({"addressing_services"}),
    "manage_firewall_rule": frozenset({"firewall_routing"}),
    "manage_ip_address": frozenset({"addressing_services", "interfaces"}),
    "manage_ip_pool": frozenset({"addressing_services"}),
    "manage_ip_service": frozenset({"addressing_services", "system"}),
    "manage_log_action": frozenset({"system"}),
    "manage_log_rule": frozenset({"system"}),
    "manage_mangle_rule": frozenset({"firewall_routing"}),
    "manage_netwatch_entry": frozenset({"addressing_services", "diagnostics"}),
    "manage_ntp_client": frozenset({"addressing_services", "system"}),
    "manage_ppp_profile": frozenset({"wan_vpn"}),
    "manage_queue": frozenset({"firewall_routing"}),
    "manage_route": frozenset({"firewall_routing"}),
    "manage_routing_rule": frozenset({"firewall_routing"}),
    "manage_routing_table": frozenset({"firewall_routing"}),
    "manage_vlan": frozenset({"interfaces"}),
    "manage_vrrp_instance": frozenset({"interfaces"}),
    "set_system_clock": frozenset({"system"}),
}


def typed_proposal_name(tool_name: str) -> str:
    return f"propose_typed_{tool_name}"


def is_approved_typed_change(tool_name: str) -> bool:
    return tool_name in APPROVED_TYPED_CHANGE_DOMAINS


_HARNESS_CONTROL_TOOLS = frozenset(
    {"apply_plan", "bulk_execute", "plan_changes", "rollback_change"}
)


class TypedDryRunFailure(RuntimeError):
    """Structured MikroMCP dry-run rejection retained for post-check semantics."""

    def __init__(self, detail: str, error_code: str | None = None) -> None:
        super().__init__(detail)
        self.error_code = error_code


def is_approval_bound_change(tool: McpTool) -> bool:
    """Whether a live router write can be represented by a confirmation-bound proposal."""

    properties = tool.input_schema.get("properties")
    return (
        tool.name not in _HARNESS_CONTROL_TOOLS
        and tool.annotations.get("readOnlyHint") is not True
        and isinstance(properties, dict)
        and "routerId" in properties
    )


class TypedChangeDefinition(RunbookDefinition):
    """Runtime runbook around one reviewed live MikroMCP write-tool schema."""

    fields = ()
    rollback_note = (
        "Rollback restores the MikroMCP snapshot captured immediately before this typed change."
    )

    def __init__(self, tool: McpTool) -> None:
        if not is_approval_bound_change(tool):
            raise ValueError(f"Tool cannot be executed through the approval workflow: {tool.name}")
        self._tool = tool
        self.id = f"typed:{tool.name}"
        self.title = (tool.description or tool.name.replace("_", " ")).split(".", 1)[0]
        self.command = f"/typed-{tool.name}"
        self.description = tool.description or f"Typed RouterOS change using {tool.name}."
        self.proposal_tool_name = typed_proposal_name(tool.name)
        self.proposal_description = (
            f"Propose, but do not directly execute, MikroMCP tool {tool.name}. "
            "The harness will dry-run the exact typed arguments, show them to the operator, "
            "and execute only after explicit approval. "
            + self.description
        )
        self.write_tools = frozenset({tool.name})
        self.capability_domains = APPROVED_TYPED_CHANGE_DOMAINS.get(tool.name, frozenset())

    @property
    def tool_name(self) -> str:
        return self._tool.name

    @classmethod
    def from_history(cls, tool_name: str) -> TypedChangeDefinition:
        return cls(
            McpTool(
                tool_name,
                tool_name.replace("_", " "),
                {
                    "type": "object",
                    "properties": {"routerId": {"type": "string"}},
                    "additionalProperties": False,
                },
                {"readOnlyHint": False, "destructiveHint": True},
            )
        )

    @property
    def proposal_tool(self) -> McpTool:
        return McpTool(
            self.proposal_tool_name,
            self.proposal_description,
            self.proposal_schema(),
            {"readOnlyHint": True, "destructiveHint": False},
        )

    def proposal_schema(self) -> dict[str, Any]:
        schema = copy.deepcopy(self._tool.input_schema)
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        for argument in _HARNESS_ARGUMENTS:
            properties.pop(argument, None)
        schema["type"] = "object"
        schema["properties"] = properties
        schema["additionalProperties"] = False
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [item for item in required if item not in _HARNESS_ARGUMENTS]
        return schema

    def sanitize_proposal(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        schema = self.proposal_schema()
        properties = cast(dict[str, Any], schema["properties"])
        unknown = sorted(set(raw) - set(properties))
        if unknown:
            raise ValueError("Unknown typed change argument(s): " + ", ".join(unknown))
        required = schema.get("required")
        required_items = required if isinstance(required, list) else []
        missing = [
            item
            for item in required_items
            if isinstance(item, str) and item not in raw
        ]
        if missing:
            raise ValueError("Missing typed change argument(s): " + ", ".join(missing))
        return dict(raw)

    def submission(self, arguments: Mapping[str, Any]) -> RunbookSubmission:
        return RunbookSubmission(
            self.id,
            {"tool": self.tool_name, "arguments": dict(arguments)},
        )

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        tool = values.get("tool")
        arguments = values.get("arguments")
        if tool != self.tool_name or not isinstance(arguments, dict):
            raise ValueError("Typed change history does not match its approved tool")
        forbidden = _HARNESS_ARGUMENTS.intersection(arguments)
        if forbidden:
            raise ValueError(
                "Typed change contains harness-owned argument(s): "
                + ", ".join(sorted(forbidden))
            )
        return (RunbookStep(self.tool_name, dict(arguments)),)

    def summary(self, values: Mapping[str, Any]) -> str:
        arguments = self.build_steps(values)[0].params
        rendered = json.dumps(arguments, ensure_ascii=False, sort_keys=True, indent=2)
        return f"Execute typed MikroMCP change {self.tool_name} with arguments:\n{rendered}"

    async def _probe(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> tuple[str, str]:
        step = self.build_steps(values)[0]
        result = await session.call_tool(
            "plan_changes",
            {"routerId": router_id, "steps": [step.as_mcp()]},
        )
        if result.is_error:
            raise RuntimeError(result.text or "Typed change dry-run failed")
        structured = result.structured_content or {}
        steps = structured.get("steps")
        if not isinstance(steps, list) or not steps or not isinstance(steps[0], dict):
            raise RuntimeError("MikroMCP returned an invalid typed change dry-run")
        first = steps[0]
        dry_run = first.get("structuredDryRun")
        if not isinstance(dry_run, dict):
            raise RuntimeError("MikroMCP omitted structured dry-run state")
        action = dry_run.get("action")
        if action == "would_fail" or not isinstance(action, str):
            raise TypedDryRunFailure(
                str(first.get("dryRunResult") or "Typed change would fail"),
                str(dry_run.get("error")) if dry_run.get("error") is not None else None,
            )
        current_state = first.get("currentState")
        fingerprint = _state_fingerprint(current_state)
        return action, fingerprint

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        action, fingerprint = await self._probe(session, router_id, values)
        return {"stateHash": fingerprint, "dryRunAction": action}

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        try:
            action, _ = await self._probe(session, router_id, values)
        except TypedDryRunFailure as error:
            requested = self.build_steps(values)[0].params.get("action")
            if (
                requested == "remove"
                and error.error_code
                and error.error_code.endswith("NOT_FOUND")
            ):
                return RunbookVerification(
                    True,
                    "Post-apply dry-run confirms that the requested record is absent.",
                )
            return RunbookVerification(False, f"Post-apply dry-run failed: {error}")
        settled = action in _SETTLED_ACTIONS
        return RunbookVerification(
            settled,
            f"Post-apply dry-run reports settled action: {action}."
            if settled
            else f"Apply succeeded, but the generic post-check still proposes action: {action}.",
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        _, fingerprint = await self._probe(session, router_id, values)
        restored = fingerprint == baseline.get("stateHash")
        return RunbookVerification(
            restored,
            "The pre-change RouterOS snapshot fingerprint was restored."
            if restored
            else "Rollback state differs from the pre-change snapshot fingerprint.",
        )


def typed_definition_for_proposal(
    catalog: tuple[McpTool, ...], proposal_name: str
) -> TypedChangeDefinition | None:
    for tool in catalog:
        if typed_proposal_name(tool.name) == proposal_name and is_approval_bound_change(tool):
            return TypedChangeDefinition(tool)
    return None


def typed_proposals_for_domains(
    catalog: tuple[McpTool, ...], domains: tuple[str, ...] | None = None
) -> tuple[McpTool, ...]:
    requested = set(domains or ())
    proposals: list[McpTool] = []
    for tool in catalog:
        tool_domains = APPROVED_TYPED_CHANGE_DOMAINS.get(tool.name)
        if not is_approval_bound_change(tool):
            continue
        if requested and tool_domains is not None and not tool_domains.intersection(requested):
            continue
        proposals.append(TypedChangeDefinition(tool).proposal_tool)
    return tuple(proposals)
