from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mth.core.mcp_client import McpTool
from mth.core.runbooks import (
    DEFAULT_RUNBOOK_REGISTRY,
    RunbookDefinition,
    RunbookRegistry,
    is_approval_bound_change,
    typed_proposals_for_domains,
)

CAPABILITY_DOMAINS = (
    "overview",
    "interfaces",
    "addressing_services",
    "firewall_routing",
    "wan_vpn",
    "system",
    "containers",
    "diagnostics",
)

# Reviewed v1.10 writes which need a specialised workflow or remain HIGH-RISK-only. New upstream
# writes stay visible as uncovered until they are deliberately classified here or approval-bound.
READY_REVIEWED_EXCLUSIONS = frozenset(
    {
        "apply_plan",
        "create_backup",
        "delete_file",
        "export_config",
        "fetch_url",
        "manage_certificate",
        "manage_container",
        "manage_container_env",
        "manage_container_mount",
        "manage_interface_list",
        "manage_interface_list_member",
        "manage_ipsec_peer",
        "manage_ipsec_policy",
        "manage_ovpn_client",
        "manage_package",
        "manage_scheduled_job",
        "manage_script",
        "manage_upgrade",
        "manage_user",
        "manage_user_group",
        "manage_wifi_interface",
        "plan_changes",
        "reboot",
        "rollback_change",
        "run_command",
        "run_script",
        "upload_file",
        "write_swos_blob",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilitySelection:
    domains: tuple[str, ...]
    tools: tuple[McpTool, ...]


@dataclass(frozen=True, slots=True)
class ReadyCapabilityContract:
    """Machine-checkable READY coverage derived from the live MCP catalog."""

    plan_reads: tuple[str, ...]
    ready_reads: tuple[str, ...]
    runbook_writes: tuple[str, ...]
    typed_writes: tuple[str, ...]
    reviewed_exclusions: tuple[str, ...]
    uncovered_writes: tuple[str, ...]
    missing_runbook_writes: tuple[str, ...]
    raw_writes_exposed: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.raw_writes_exposed

    @property
    def complete(self) -> bool:
        return self.safe and not self.uncovered_writes and not self.missing_runbook_writes

    def as_dict(self) -> dict[str, bool | list[str]]:
        return {
            "safe": self.safe,
            "complete": self.complete,
            "plan_reads": list(self.plan_reads),
            "ready_reads": list(self.ready_reads),
            "runbook_writes": list(self.runbook_writes),
            "typed_writes": list(self.typed_writes),
            "reviewed_exclusions": list(self.reviewed_exclusions),
            "uncovered_writes": list(self.uncovered_writes),
            "missing_runbook_writes": list(self.missing_runbook_writes),
            "raw_writes_exposed": list(self.raw_writes_exposed),
        }


class ToolCatalogRouter:
    """Routes the live MCP catalog into small, safe domain packs."""

    # Trust MikroMCP annotations for broad read coverage (e.g. torch), while
    # retaining a small deny-list against a malformed annotation on an obvious
    # mutation/control primitive.
    READ_ONLY_FORBIDDEN_PREFIXES = (
        "manage_",
        "set_",
        "create_",
        "delete_",
        "run_",
        "write_",
    )
    READ_ONLY_FORBIDDEN_EXACT = frozenset(
        {"apply_plan", "rollback_change", "bulk_execute", "reboot", "fetch_url", "upload_file"}
    )
    BASE_TOOLS = frozenset({"check_router_health", "get_system_status"})
    SELECTOR_NAME = "select_router_capabilities"

    def __init__(self, registry: RunbookRegistry = DEFAULT_RUNBOOK_REGISTRY) -> None:
        self.registry = registry

    @property
    def selector_tool(self) -> McpTool:
        return McpTool(
            self.SELECTOR_NAME,
            (
                "Load one or more small RouterOS capability packs before inspecting live router "
                "state or proposing a change. Choose only domains relevant to the user's task. "
                "This is local catalog routing and never contacts or changes the router."
            ),
            {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(CAPABILITY_DOMAINS)},
                        "minItems": 1,
                        "maxItems": 3,
                        "description": "Relevant RouterOS capability domains",
                    }
                },
                "required": ["domains"],
                "additionalProperties": False,
            },
            {"readOnlyHint": True, "destructiveHint": False},
        )

    def select(
        self,
        catalog: Sequence[McpTool],
        raw_domains: Any,
    ) -> CapabilitySelection:
        domains = self._normalize_domains(raw_domains)
        safe = self.filter_read_only(catalog)
        live_names = {tool.name for tool in catalog}
        selected = tuple(
            self._for_model(tool)
            for tool in safe
            if tool.name in self.BASE_TOOLS
            or any(self._belongs(tool.name, domain) for domain in domains)
        )
        proposal_tools = tuple(
            self._proposal_tool(definition)
            for definition in self.registry.all()
            if definition.capability_domains.intersection(domains)
            and definition.write_tools.issubset(live_names)
        )
        typed_proposals = typed_proposals_for_domains(tuple(catalog), domains)
        by_name = {self.selector_tool.name: self.selector_tool}
        for tool in selected:
            by_name.setdefault(tool.name, tool)
        for tool in (*proposal_tools, *typed_proposals):
            by_name[tool.name] = tool
        return CapabilitySelection(
            domains=domains,
            tools=tuple(by_name.values()),
        )

    def plan_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """All live RouterOS reads are available for PLAN-mode reconnaissance."""

        return tuple(self._for_model(tool) for tool in self.filter_read_only(catalog))

    def ready_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """Expose every safe read plus approval wrappers for reviewed write schemas."""

        live = tuple(catalog)
        live_names = {tool.name for tool in live}
        scenario_proposals = tuple(
            self._proposal_tool(definition)
            for definition in self.registry.all()
            if definition.write_tools.issubset(live_names)
        )
        generic_proposals = typed_proposals_for_domains(live, None)
        by_name = {
            tool.name: self._for_model(tool) for tool in self.filter_read_only(live)
        }
        for tool in (*scenario_proposals, *generic_proposals):
            by_name[tool.name] = tool
        return tuple(by_name.values())

    def ready_contract(self, catalog: Sequence[McpTool]) -> ReadyCapabilityContract:
        """Describe live READY coverage without assuming a backend tool count."""

        live = tuple(catalog)
        live_names = {tool.name for tool in live}
        reads = self.filter_read_only(live)
        read_names = {tool.name for tool in reads}
        router_writes = {
            tool.name
            for tool in live
            if self._is_router_bound(tool) and tool.name not in read_names
        }
        available_runbooks = tuple(
            definition
            for definition in self.registry.all()
            if definition.write_tools.issubset(live_names)
        )
        runbook_writes = {
            name for definition in available_runbooks for name in definition.write_tools
        }
        typed_writes = {tool.name for tool in live if is_approval_bound_change(tool)}
        reviewed_exclusions = router_writes.intersection(READY_REVIEWED_EXCLUSIONS)
        ready_names = {tool.name for tool in self.ready_tools(live)}
        ready_reads = read_names.intersection(ready_names)
        required_runbook_writes = set(self.registry.write_tools)
        return ReadyCapabilityContract(
            plan_reads=tuple(sorted(read_names)),
            ready_reads=tuple(sorted(ready_reads)),
            runbook_writes=tuple(sorted(runbook_writes)),
            typed_writes=tuple(sorted(typed_writes)),
            reviewed_exclusions=tuple(sorted(reviewed_exclusions)),
            uncovered_writes=tuple(
                sorted(
                    router_writes
                    - runbook_writes
                    - typed_writes
                    - reviewed_exclusions
                )
            ),
            missing_runbook_writes=tuple(sorted(required_runbook_writes - live_names)),
            raw_writes_exposed=tuple(sorted(router_writes.intersection(ready_names))),
        )

    def high_risk_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """Expose the complete live catalog plus READY's helpful proposal vocabulary.

        HIGH RISK deliberately does not route a direct MikroMCP write through a proposal:
        the user has already explicitly entered the mode which removes that per-call gate.
        Proposal tools remain available as an optional structured planning convenience.
        """

        by_name = {tool.name: self._for_model(tool) for tool in catalog}
        for tool in (*self.ready_tools(catalog), self.ssh_exec_tool):
            by_name[tool.name] = tool
        return tuple(by_name.values())

    @staticmethod
    def _for_model(tool: McpTool) -> McpTool:
        """Hide connection/confirmation fields owned and injected by the harness."""

        schema = dict(tool.input_schema)
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return tool
        visible = dict(properties)
        visible.pop("routerId", None)
        visible.pop("confirmationToken", None)
        schema["properties"] = visible
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [
                name for name in required if name not in {"routerId", "confirmationToken"}
            ]
        return McpTool(tool.name, tool.description, schema, dict(tool.annotations))

    @property
    def ssh_exec_tool(self) -> McpTool:
        return McpTool(
            "ssh_exec",
            (
                "Execute exactly one RouterOS CLI line through the persistent HIGH RISK SSH "
                "session. It has no per-command approval gate. Inspect state first and use it "
                "only for the user's requested task; never include passwords in the command."
            ),
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "description": "One RouterOS CLI command line without line breaks",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 120,
                        "default": 20,
                    },
                    "max_output_bytes": {
                        "type": "integer",
                        "minimum": 256,
                        "maximum": 2000000,
                        "default": 65536,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            {"readOnlyHint": False, "destructiveHint": True},
        )

    @classmethod
    def filter_read_only(cls, tools: Sequence[McpTool]) -> tuple[McpTool, ...]:
        return tuple(tool for tool in tools if cls.is_router_bound_read_tool(tool))

    @classmethod
    def is_router_bound_read_tool(cls, tool: McpTool) -> bool:
        obvious_write = tool.name in cls.READ_ONLY_FORBIDDEN_EXACT or tool.name.startswith(
            cls.READ_ONLY_FORBIDDEN_PREFIXES
        )
        return (
            cls._is_router_bound(tool)
            and not obvious_write
            and tool.annotations.get("readOnlyHint") is True
            and tool.annotations.get("destructiveHint") is not True
        )

    @staticmethod
    def _is_router_bound(tool: McpTool) -> bool:
        properties = tool.input_schema.get("properties")
        return isinstance(properties, dict) and "routerId" in properties

    @staticmethod
    def _normalize_domains(raw: Any) -> tuple[str, ...]:
        values = raw if isinstance(raw, list) else []
        normalized: list[str] = []
        for value in values:
            if isinstance(value, str) and value in CAPABILITY_DOMAINS and value not in normalized:
                normalized.append(value)
        return tuple(normalized or ("overview",))

    @staticmethod
    def _belongs(name: str, domain: str) -> bool:
        tokens: Mapping[str, tuple[str, ...]] = {
            "overview": ("system_status", "router_health", "interfaces", "routes"),
            "interfaces": (
                "interface",
                "bridge",
                "neighbor",
                "arp_",
                "vrrp",
                "swos",
            ),
            "addressing_services": (
                "ip_address",
                "dhcp",
                "dns",
                "ip_pool",
                "ip_service",
                "netwatch",
                "ntp",
                "snmp",
            ),
            "firewall_routing": (
                "firewall",
                "mangle",
                "address_list",
                "connection",
                "route",
                "routing",
                "bgp",
                "ospf",
                "queue",
            ),
            "wan_vpn": ("wifi", "wireguard", "ipsec", "ppp", "ovpn"),
            "system": (
                "system",
                "clock",
                "log",
                "script",
                "scheduled",
                "package",
                "file",
                "certificate",
                "user",
                "upgrade",
            ),
            "containers": ("container",),
            "diagnostics": (
                "router_health",
                "system_status",
                "ping",
                "traceroute",
                "log",
                "interface",
                "route",
                "dns",
                "firewall",
                "connection",
            ),
        }
        return any(token in name for token in tokens[domain])

    @staticmethod
    def _proposal_tool(definition: RunbookDefinition) -> McpTool:
        return McpTool(
            definition.proposal_tool_name,
            definition.proposal_description,
            definition.proposal_schema(),
            {"readOnlyHint": True, "destructiveHint": False},
        )
