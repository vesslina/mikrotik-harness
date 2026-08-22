from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mth.core.mcp_client import McpTool
from mth.core.runbooks import (
    DEFAULT_RUNBOOK_REGISTRY,
    RunbookDefinition,
    RunbookRegistry,
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


@dataclass(frozen=True, slots=True)
class CapabilitySelection:
    domains: tuple[str, ...]
    tools: tuple[McpTool, ...]


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
        selected = tuple(
            tool
            for tool in safe
            if tool.name in self.BASE_TOOLS
            or any(self._belongs(tool.name, domain) for domain in domains)
        )
        proposal_tools = tuple(
            self._proposal_tool(definition)
            for definition in self.registry.all()
            if definition.capability_domains.intersection(domains)
        )
        typed_proposals = typed_proposals_for_domains(tuple(catalog), domains)
        return CapabilitySelection(
            domains=domains,
            tools=(self.selector_tool, *selected, *proposal_tools, *typed_proposals),
        )

    def plan_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """All live RouterOS reads are available for PLAN-mode reconnaissance."""

        return self.filter_read_only(catalog)

    def ready_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """Expose every safe read plus an approval wrapper for every router-bound write."""

        live = tuple(catalog)
        scenario_proposals = tuple(
            self._proposal_tool(definition) for definition in self.registry.all()
        )
        generic_proposals = typed_proposals_for_domains(live, None)
        by_name: dict[str, McpTool] = {}
        for tool in (*self.filter_read_only(live), *scenario_proposals, *generic_proposals):
            by_name.setdefault(tool.name, tool)
        return tuple(by_name.values())

    def high_risk_tools(self, catalog: Sequence[McpTool]) -> tuple[McpTool, ...]:
        """Expose the complete live catalog plus READY's helpful proposal vocabulary.

        HIGH RISK deliberately does not route a direct MikroMCP write through a proposal:
        the user has already explicitly entered the mode which removes that per-call gate.
        Proposal tools remain available as an optional structured planning convenience.
        """

        by_name: dict[str, McpTool] = {}
        for tool in (*catalog, *self.ready_tools(catalog), self.ssh_exec_tool):
            by_name.setdefault(tool.name, tool)
        return tuple(by_name.values())

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
        properties = tool.input_schema.get("properties")
        router_bound = isinstance(properties, dict) and "routerId" in properties
        obvious_write = tool.name in cls.READ_ONLY_FORBIDDEN_EXACT or tool.name.startswith(
            cls.READ_ONLY_FORBIDDEN_PREFIXES
        )
        return (
            router_bound
            and not obvious_write
            and tool.annotations.get("readOnlyHint") is True
            and tool.annotations.get("destructiveHint") is not True
        )

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
