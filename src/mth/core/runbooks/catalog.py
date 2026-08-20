from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from mth.core.mcp_client import McpToolResult
from mth.core.runbooks.base import (
    RunbookDefinition,
    RunbookField,
    RunbookFieldKind,
    RunbookStep,
    RunbookVerification,
    ToolSession,
)
from mth.core.runbooks.extended import (
    AddressListEntryDefinition,
    DhcpServerDefinition,
    DnsResolverDefinition,
    IpAddressDefinition,
    WireGuardPeerDefinition,
)


def _records(result: McpToolResult, key: str) -> list[dict[str, Any]]:
    if result.is_error:
        raise RuntimeError(result.text or f"MikroMCP failed to read {key}")
    structured = result.structured_content or {}
    raw = structured.get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _string(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    return value if isinstance(value, str) else ""


def _boolean(values: Mapping[str, Any], name: str) -> bool:
    return values.get(name) is True


def _strings(values: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = values.get(name)
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "yes", "1", "enabled"}


def _matching_record(
    records: Iterable[dict[str, Any]],
    field: str,
    value: str,
) -> dict[str, Any] | None:
    return next((record for record in records if record.get(field) == value), None)


def _project(
    record: Mapping[str, Any] | None,
    fields: Iterable[str],
) -> dict[str, Any] | None:
    """Persist only fields required for rollback verification, never arbitrary device data."""

    if record is None:
        return None
    return {field: record.get(field) for field in fields}


class WanPppoeDefinition(RunbookDefinition):
    id = "wan_pppoe"
    title = "WAN PPPoE"
    command = "/pppoe"
    description = "Create an idempotent WAN PPPoE client."
    proposal_tool_name = "propose_wan_pppoe"
    proposal_description = (
        "Propose the harness-owned WAN PPPoE runbook when the user wants to add or "
        "configure a PPPoE client. This never changes RouterOS. Never request or pass "
        "a password; the harness collects it later in a masked human form."
    )
    fields = (
        RunbookField("name", "Client name", required=True, default="pppoe-wan", max_length=64),
        RunbookField(
            "interface", "Parent interface", required=True, default="ether1", max_length=64
        ),
        RunbookField("username", "ISP username", required=True, max_length=256),
        RunbookField(
            "password",
            "ISP password",
            kind=RunbookFieldKind.SECRET,
            required=True,
            max_length=1024,
        ),
        RunbookField("serviceName", "Service name", placeholder="optional", max_length=256),
        RunbookField(
            "addDefaultRoute",
            "Add default route",
            kind=RunbookFieldKind.BOOLEAN,
            default=True,
        ),
        RunbookField(
            "dialOnDemand",
            "Dial on demand",
            kind=RunbookFieldKind.BOOLEAN,
            default=False,
        ),
    )
    write_tools = frozenset({"manage_pppoe_client"})
    secret_backend_parameters = frozenset({"password"})
    capability_domains = frozenset({"wan_vpn"})

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        params: dict[str, Any] = {
            "action": "add",
            "name": _string(values, "name"),
            "interface": _string(values, "interface"),
            "user": _string(values, "username"),
            "addDefaultRoute": _boolean(values, "addDefaultRoute"),
            "dialOnDemand": _boolean(values, "dialOnDemand"),
        }
        service_name = _string(values, "serviceName")
        if service_name:
            params["serviceName"] = service_name
        if secrets is not None:
            password = secrets.get("password")
            if not password:
                raise ValueError("ISP password must not be empty")
            params["password"] = password
        return (RunbookStep("manage_pppoe_client", params),)

    def summary(self, values: Mapping[str, Any]) -> str:
        route = "yes" if _boolean(values, "addDefaultRoute") else "no"
        demand = "yes" if _boolean(values, "dialOnDemand") else "no"
        service = _string(values, "serviceName") or "any"
        return (
            f'Create PPPoE client "{_string(values, "name")}" on '
            f'{_string(values, "interface")}\n'
            f'User: {_string(values, "username")} · service: {service}\n'
            f"Default route: {route} · dial on demand: {demand}\n"
            "Password: supplied through the masked field (not shown)"
        )

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        result = await session.call_tool(
            "list_pppoe_clients", {"routerId": router_id, "status": "all", "limit": 500}
        )
        record = _matching_record(_records(result, "clients"), "name", _string(values, "name"))
        return {
            "record": _project(
                record,
                (
                    "name",
                    "interface",
                    "user",
                    "service-name",
                    "add-default-route",
                    "dial-on-demand",
                    "disabled",
                ),
            )
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        result = await session.call_tool(
            "list_pppoe_clients", {"routerId": router_id, "status": "all", "limit": 500}
        )
        if result.is_error:
            return RunbookVerification(False, f"PPPoE post-check failed: {result.text}")
        record = _matching_record(_records(result, "clients"), "name", _string(values, "name"))
        if record is None:
            return RunbookVerification(False, "PPPoE client was not found after apply.")
        if (
            record.get("interface") != _string(values, "interface")
            or record.get("user") != _string(values, "username")
        ):
            return RunbookVerification(False, "PPPoE client differs from the approved plan.")
        if _as_bool(record.get("disabled")):
            return RunbookVerification(
                True, "PPPoE configuration matches; the interface is disabled.", False
            )
        running = record.get("running")
        if _as_bool(running):
            return RunbookVerification(True, "PPPoE configuration matches and is active.", True)
        if running is not None:
            return RunbookVerification(
                True,
                "PPPoE configuration matches; the session is not currently running.",
                False,
            )
        return RunbookVerification(
            True, "PPPoE configuration matches; operational state was not reported.", None
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        result = await session.call_tool(
            "list_pppoe_clients", {"routerId": router_id, "status": "all", "limit": 500}
        )
        if result.is_error:
            return RunbookVerification(False, f"PPPoE rollback post-check failed: {result.text}")
        current = _matching_record(_records(result, "clients"), "name", _string(values, "name"))
        before = baseline.get("record")
        if before is None and current is None:
            return RunbookVerification(True, "PPPoE client is absent; rollback verified.")
        if isinstance(before, dict) and current is not None:
            same = all(current.get(key) == before.get(key) for key in ("name", "interface", "user"))
            return RunbookVerification(
                same,
                "Original PPPoE configuration was restored."
                if same
                else "PPPoE configuration does not match the pre-change baseline.",
            )
        return RunbookVerification(False, "PPPoE rollback does not match the pre-change state.")


class LanBridgeDefinition(RunbookDefinition):
    id = "lan_bridge"
    title = "LAN bridge"
    command = "/bridge"
    description = "Create a LAN bridge and add selected interface ports."
    proposal_tool_name = "propose_lan_bridge"
    proposal_description = (
        "Propose the harness-owned LAN bridge runbook. Supply the bridge name and only "
        "interfaces explicitly named by the user. This does not change RouterOS."
    )
    fields = (
        RunbookField("name", "Bridge name", required=True, default="bridge-lan", max_length=15),
        RunbookField(
            "interfaces",
            "Member interfaces",
            kind=RunbookFieldKind.CSV,
            required=True,
            placeholder="ether2, ether3, ether4",
            description="Interfaces to add as bridge ports",
        ),
        RunbookField("comment", "Comment", default="Managed by mth", max_length=255),
        RunbookField(
            "disabled", "Create disabled", kind=RunbookFieldKind.BOOLEAN, default=False
        ),
    )
    write_tools = frozenset({"manage_bridge", "manage_bridge_port"})
    capability_domains = frozenset({"interfaces"})

    def validate(self, values: Mapping[str, Any]) -> None:
        interfaces = _strings(values, "interfaces")
        if len(interfaces) > 9:
            raise ValueError("A bridge runbook supports at most 9 ports per approved plan")
        if _string(values, "name") in interfaces:
            raise ValueError("The bridge cannot be one of its own member interfaces")

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        bridge_params: dict[str, Any] = {
            "action": "create",
            "name": _string(values, "name"),
            "disabled": _boolean(values, "disabled"),
        }
        comment = _string(values, "comment")
        if comment:
            bridge_params["comment"] = comment
        return (
            RunbookStep("manage_bridge", bridge_params),
            *(
                RunbookStep(
                    "manage_bridge_port",
                    {
                        "action": "add",
                        "bridge": _string(values, "name"),
                        "interface": interface,
                    },
                )
                for interface in _strings(values, "interfaces")
            ),
        )

    def summary(self, values: Mapping[str, Any]) -> str:
        ports = ", ".join(_strings(values, "interfaces"))
        state = "disabled" if _boolean(values, "disabled") else "enabled"
        return (
            f'Create LAN bridge "{_string(values, "name")}" ({state})\n'
            f"Add ports: {ports}\n"
            "Moving a management-facing interface into a bridge can interrupt connectivity."
        )

    async def _current(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await session.call_tool("list_bridges", {"routerId": router_id, "limit": 500})
        return _matching_record(_records(result, "bridges"), "name", _string(values, "name"))

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        record = await self._current(session, router_id, values)
        safe = _project(record, ("name", "comment", "disabled"))
        if safe is not None:
            ports = record.get("ports", []) if record is not None else []
            safe["ports"] = [
                {"interface": port.get("interface")}
                for port in ports
                if isinstance(port, dict) and isinstance(port.get("interface"), str)
            ]
        return {"record": safe}

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        record = await self._current(session, router_id, values)
        if record is None:
            return RunbookVerification(False, "LAN bridge was not found after apply.")
        members = {
            str(port.get("interface"))
            for port in record.get("ports", [])
            if isinstance(port, dict) and port.get("interface")
        }
        missing = set(_strings(values, "interfaces")) - members
        if missing:
            return RunbookVerification(
                False, "Bridge exists, but these ports are missing: " + ", ".join(sorted(missing))
            )
        return RunbookVerification(
            True,
            f'Bridge "{_string(values, "name")}" and all requested ports are present.',
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        current = await self._current(session, router_id, values)
        before = baseline.get("record")
        if before is None and current is None:
            return RunbookVerification(True, "Bridge is absent; rollback verified.")
        if not isinstance(before, dict) or current is None:
            return RunbookVerification(False, "Bridge rollback does not match the baseline.")
        before_ports = {
            str(port.get("interface"))
            for port in before.get("ports", [])
            if isinstance(port, dict) and port.get("interface")
        }
        current_ports = {
            str(port.get("interface"))
            for port in current.get("ports", [])
            if isinstance(port, dict) and port.get("interface")
        }
        matches = before_ports == current_ports
        return RunbookVerification(
            matches,
            "Original bridge port membership was restored."
            if matches
            else "Bridge port membership differs from the pre-change baseline.",
        )


class WanNatDefinition(RunbookDefinition):
    id = "wan_nat"
    title = "WAN NAT masquerade"
    command = "/nat"
    description = "Add an idempotent source NAT masquerade rule for a WAN interface."
    proposal_tool_name = "propose_wan_nat"
    proposal_description = (
        "Propose the harness-owned WAN source NAT masquerade runbook. Use only for "
        "masquerade; port-forwarding is not supported by the pinned backend tool."
    )
    fields = (
        RunbookField("outInterface", "WAN interface", required=True, default="pppoe-wan"),
        RunbookField("srcAddress", "Source network", placeholder="optional, e.g. 192.168.88.0/24"),
        RunbookField("comment", "Rule comment", required=True, default="mth: wan masquerade"),
        RunbookField("disabled", "Create disabled", kind=RunbookFieldKind.BOOLEAN, default=False),
    )
    write_tools = frozenset({"manage_firewall_rule"})
    capability_domains = frozenset({"firewall_routing", "wan_vpn"})
    rollback_note = (
        "Rollback restores rule presence and fields, but RouterOS firewall rule order is not "
        "guaranteed to return to its original position."
    )

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        params: dict[str, Any] = {
            "table": "nat",
            "action": "add",
            "chain": "srcnat",
            "ruleAction": "masquerade",
            "outInterface": _string(values, "outInterface"),
            "comment": _string(values, "comment"),
            "disabled": _boolean(values, "disabled"),
        }
        source = _string(values, "srcAddress")
        if source:
            params["srcAddress"] = source
        return (RunbookStep("manage_firewall_rule", params),)

    def summary(self, values: Mapping[str, Any]) -> str:
        source = _string(values, "srcAddress") or "any source"
        return (
            f"Add srcnat masquerade through {_string(values, 'outInterface')}\n"
            f"Source: {source}\nComment: {_string(values, 'comment')}\n"
            "Firewall rollback cannot guarantee the original rule order."
        )

    async def _current(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await session.call_tool(
            "list_firewall_rules",
            {"routerId": router_id, "table": "nat", "disabled": "all", "limit": 500},
        )
        return _matching_record(_records(result, "rules"), "comment", _string(values, "comment"))

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        record = await self._current(session, router_id, values)
        return {
            "record": _project(
                record,
                (
                    "chain",
                    "action",
                    "out-interface",
                    "src-address",
                    "comment",
                    "disabled",
                ),
            )
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        record = await self._current(session, router_id, values)
        if record is None:
            return RunbookVerification(False, "NAT rule was not found after apply.")
        expected = {
            "chain": "srcnat",
            "action": "masquerade",
            "out-interface": _string(values, "outInterface"),
            "src-address": _string(values, "srcAddress"),
        }
        matches = all((record.get(key) or "") == value for key, value in expected.items())
        return RunbookVerification(
            matches,
            "WAN masquerade rule matches the approved plan."
            if matches
            else "NAT rule exists but differs from the approved plan.",
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        current = await self._current(session, router_id, values)
        before = baseline.get("record")
        if before is None and current is None:
            return RunbookVerification(True, "NAT rule is absent; rollback verified.")
        if not isinstance(before, dict) or current is None:
            return RunbookVerification(False, "NAT rollback does not match the baseline.")
        keys = ("chain", "action", "out-interface", "src-address", "comment")
        matches = all(current.get(key) == before.get(key) for key in keys)
        return RunbookVerification(
            matches,
            "Original NAT rule fields were restored."
            if matches
            else "NAT rule fields differ from the pre-change baseline.",
        )


class AdminServicesDefinition(RunbookDefinition):
    id = "admin_services"
    title = "Disable administrative services"
    command = "/services"
    description = "Disable unnecessary RouterOS administrative services without lockout."
    proposal_tool_name = "propose_admin_services"
    proposal_description = (
        "Propose disabling unnecessary RouterOS administrative services. Never include "
        "www-ssl, ssh, or winbox; the harness and operator may depend on them."
    )
    safe_services = ("api", "api-ssl", "telnet", "www", "ftp")
    fields = (
        RunbookField(
            "services",
            "Services to disable",
            kind=RunbookFieldKind.CSV,
            required=True,
            default=safe_services,
            allowed_values=safe_services,
            placeholder="api, api-ssl, telnet, www, ftp",
        ),
    )
    write_tools = frozenset({"manage_ip_service"})
    capability_domains = frozenset({"addressing_services", "system"})

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        return tuple(
            RunbookStep("manage_ip_service", {"action": "disable", "name": service})
            for service in _strings(values, "services")
        )

    def summary(self, values: Mapping[str, Any]) -> str:
        services = ", ".join(_strings(values, "services"))
        return (
            f"Disable RouterOS services: {services}\n"
            "www-ssl, ssh, and winbox are protected from this runbook to prevent lockout."
        )

    async def _current(
        self, session: ToolSession, router_id: str
    ) -> dict[str, dict[str, Any]]:
        result = await session.call_tool("list_ip_services", {"routerId": router_id})
        return {
            str(record.get("name")): record
            for record in _records(result, "services")
            if record.get("name")
        }

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        current = await self._current(session, router_id)
        return {
            "disabled": {
                service: _as_bool(current.get(service, {}).get("disabled"))
                for service in _strings(values, "services")
            }
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        current = await self._current(session, router_id)
        not_disabled = [
            service
            for service in _strings(values, "services")
            if not _as_bool(current.get(service, {}).get("disabled"))
        ]
        if not_disabled:
            return RunbookVerification(
                False, "Services still enabled: " + ", ".join(not_disabled)
            )
        return RunbookVerification(True, "All approved administrative services are disabled.")

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        current = await self._current(session, router_id)
        expected = baseline.get("disabled")
        if not isinstance(expected, dict):
            return RunbookVerification(False, "Service baseline is missing.")
        mismatches = [
            service
            for service in _strings(values, "services")
            if _as_bool(current.get(service, {}).get("disabled"))
            != _as_bool(expected.get(service))
        ]
        if mismatches:
            return RunbookVerification(
                False, "Service states differ from baseline: " + ", ".join(mismatches)
            )
        return RunbookVerification(True, "Original administrative service states were restored.")


class RunbookRegistry:
    def __init__(self, definitions: Iterable[RunbookDefinition]) -> None:
        self._definitions = tuple(definitions)
        ids = [definition.id for definition in self._definitions]
        commands = [definition.command for definition in self._definitions]
        proposals = [definition.proposal_tool_name for definition in self._definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("runbook IDs must be unique")
        if len(commands) != len(set(commands)):
            raise ValueError("runbook commands must be unique")
        if len(proposals) != len(set(proposals)):
            raise ValueError("runbook proposal tool names must be unique")

    def all(self) -> tuple[RunbookDefinition, ...]:
        return self._definitions

    def get(self, runbook_id: str) -> RunbookDefinition:
        definition = next(
            (item for item in self._definitions if item.id == runbook_id), None
        )
        if definition is None:
            raise KeyError(runbook_id)
        return definition

    def for_command(self, command: str) -> RunbookDefinition | None:
        return next((item for item in self._definitions if item.command == command), None)

    def for_proposal(self, tool_name: str) -> RunbookDefinition | None:
        return next(
            (item for item in self._definitions if item.proposal_tool_name == tool_name), None
        )

    @property
    def write_tools(self) -> tuple[str, ...]:
        return tuple(sorted({tool for item in self._definitions for tool in item.write_tools}))


DEFAULT_RUNBOOK_REGISTRY = RunbookRegistry(
    (
        WanPppoeDefinition(),
        LanBridgeDefinition(),
        IpAddressDefinition(),
        AddressListEntryDefinition(),
        DhcpServerDefinition(),
        DnsResolverDefinition(),
        WanNatDefinition(),
        AdminServicesDefinition(),
        WireGuardPeerDefinition(),
    )
)
