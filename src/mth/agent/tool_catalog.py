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

    READ_ONLY_EXACT = frozenset({"check_router_health", "ping", "traceroute"})
    READ_ONLY_PREFIXES = ("list_", "get_")
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

    @classmethod
    def filter_read_only(cls, tools: Sequence[McpTool]) -> tuple[McpTool, ...]:
        return tuple(tool for tool in tools if cls.is_router_bound_read_tool(tool))

    @classmethod
    def is_router_bound_read_tool(cls, tool: McpTool) -> bool:
        name_allowed = (
            tool.name in cls.READ_ONLY_EXACT
            or tool.name.startswith(cls.READ_ONLY_PREFIXES)
        )
        properties = tool.input_schema.get("properties")
        router_bound = isinstance(properties, dict) and "routerId" in properties
        return (
            name_allowed
            and router_bound
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
