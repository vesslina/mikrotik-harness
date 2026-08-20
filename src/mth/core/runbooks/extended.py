from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
from ipaddress import ip_address, ip_interface, ip_network
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


def _records(result: McpToolResult, key: str) -> list[dict[str, Any]]:
    if result.is_error:
        raise RuntimeError(result.text or f"MikroMCP failed to read {key}")
    raw = (result.structured_content or {}).get(key)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _record(result: McpToolResult, key: str) -> dict[str, Any]:
    if result.is_error:
        raise RuntimeError(result.text or f"MikroMCP failed to read {key}")
    raw = (result.structured_content or {}).get(key)
    return dict(raw) if isinstance(raw, dict) else {}


def _matching(
    records: Iterable[dict[str, Any]], field: str, value: str
) -> dict[str, Any] | None:
    return next((item for item in records if item.get(field) == value), None)


def _project(
    record: Mapping[str, Any] | None, fields: Iterable[str]
) -> dict[str, Any] | None:
    if record is None:
        return None
    return {field: record.get(field) for field in fields}


def _string(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    return value if isinstance(value, str) else ""


def _strings(values: Mapping[str, Any], name: str) -> tuple[str, ...]:
    raw = values.get(name)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(item for item in raw if isinstance(item, str))


def _boolean(values: Mapping[str, Any], name: str) -> bool:
    return values.get(name) is True


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"true", "yes", "1", "enabled"}


class IpAddressDefinition(RunbookDefinition):
    """Assign one IPv4 address to an existing RouterOS interface."""

    id = "ip_address"
    title = "Interface IP address"
    command = "/ip-address"
    description = "Add an IPv4 address to an existing RouterOS interface."
    proposal_tool_name = "propose_ip_address"
    proposal_description = (
        "Propose the approval-bound runbook that adds an IPv4 address/CIDR to an "
        "existing RouterOS interface. Use this for /ip address, not a firewall address list."
    )
    fields = (
        RunbookField(
            "address",
            "IPv4 address/CIDR",
            required=True,
            default="192.168.88.1/24",
            description="IPv4 interface address in CIDR notation, for example 192.168.1.33/24",
        ),
        RunbookField(
            "interface",
            "Interface name",
            required=True,
            default="bridge-lan",
            description="Existing RouterOS interface that will receive the address",
        ),
        RunbookField("comment", "Comment", default="Managed by mth", max_length=255),
        RunbookField(
            "disabled",
            "Create disabled",
            kind=RunbookFieldKind.BOOLEAN,
            default=False,
        ),
    )
    write_tools = frozenset({"manage_ip_address"})
    capability_domains = frozenset({"addressing_services", "interfaces"})

    def validate(self, values: Mapping[str, Any]) -> None:
        try:
            address = ip_interface(_string(values, "address"))
        except ValueError as error:
            raise ValueError("IPv4 address must use valid CIDR notation") from error
        if address.version != 4:
            raise ValueError("This runbook currently supports IPv4 addresses only")

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        params: dict[str, Any] = {
            "action": "add",
            "address": _string(values, "address"),
            "interface": _string(values, "interface"),
            "disabled": _boolean(values, "disabled"),
        }
        comment = _string(values, "comment")
        if comment:
            params["comment"] = comment
        return (RunbookStep("manage_ip_address", params),)

    def summary(self, values: Mapping[str, Any]) -> str:
        state = "disabled" if _boolean(values, "disabled") else "enabled"
        return (
            f'Add {_string(values, "address")} to interface '
            f'"{_string(values, "interface")}" ({state}).'
        )

    async def _present(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> bool:
        params = dict(self.build_steps(values)[0].params)
        result = await session.call_tool(
            "manage_ip_address", {"routerId": router_id, **params, "dryRun": True}
        )
        if result.is_error:
            raise RuntimeError(result.text or "Could not inspect the target IP address")
        return (result.structured_content or {}).get("action") == "already_exists"

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {"present": await self._present(session, router_id, values)}

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        present = await self._present(session, router_id, values)
        return RunbookVerification(
            present,
            "The approved IPv4 address is present on the target interface."
            if present
            else "The approved IPv4 address is absent after apply.",
            present and not _boolean(values, "disabled"),
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        present = await self._present(session, router_id, values)
        matches = present == (baseline.get("present") is True)
        return RunbookVerification(
            matches,
            "The original IPv4 address state was restored."
            if matches
            else "IPv4 address rollback differs from the pre-change baseline.",
        )


class AddressListEntryDefinition(RunbookDefinition):
    """Add one permanent or expiring IPv4 firewall address-list entry."""

    id = "address_list_entry"
    title = "Firewall address-list entry"
    command = "/address-list"
    description = "Add an IPv4 address or CIDR to a RouterOS firewall address list."
    proposal_tool_name = "propose_address_list_entry"
    proposal_description = (
        "Propose the approval-bound runbook that adds an IPv4 address/CIDR to a RouterOS "
        "firewall address list. This does not assign the address to an interface."
    )
    fields = (
        RunbookField(
            "list",
            "Address-list name",
            required=True,
            default="managed-by-mth",
        ),
        RunbookField(
            "address",
            "IPv4 address/CIDR",
            required=True,
            default="192.168.1.33/32",
        ),
        RunbookField("comment", "Comment", default="Managed by mth", max_length=255),
        RunbookField("timeout", "Timeout", placeholder="optional, for example 1d"),
    )
    write_tools = frozenset({"manage_address_list_entry"})
    capability_domains = frozenset({"firewall_routing"})

    def validate(self, values: Mapping[str, Any]) -> None:
        try:
            address = ip_network(_string(values, "address"), strict=False)
        except ValueError as error:
            raise ValueError("Address-list entry must be a valid IPv4 address or CIDR") from error
        if address.version != 4:
            raise ValueError("This runbook currently supports IPv4 address lists only")

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        params: dict[str, Any] = {
            "action": "add",
            "list": _string(values, "list"),
            "address": _string(values, "address"),
        }
        for field in ("comment", "timeout"):
            value = _string(values, field)
            if value:
                params[field] = value
        return (RunbookStep("manage_address_list_entry", params),)

    def summary(self, values: Mapping[str, Any]) -> str:
        return (
            f'Add {_string(values, "address")} to firewall address list '
            f'"{_string(values, "list")}".'
        )

    async def _current(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        result = await session.call_tool(
            "list_address_list_entries",
            {
                "routerId": router_id,
                "list": _string(values, "list"),
                "address": _string(values, "address"),
            },
        )
        return _matching(
            _records(result, "entries"), "address", _string(values, "address")
        )

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        current = await self._current(session, router_id, values)
        return {
            "entry": _project(
                current, ("list", "address", "timeout", "disabled", "comment")
            )
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        present = await self._current(session, router_id, values) is not None
        return RunbookVerification(
            present,
            "The approved firewall address-list entry is present."
            if present
            else "The approved firewall address-list entry is absent after apply.",
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        current = await self._current(session, router_id, values)
        before = baseline.get("entry")
        matches = (before is None and current is None) or (
            isinstance(before, dict)
            and current is not None
            and all(current.get(key) == value for key, value in before.items())
        )
        return RunbookVerification(
            matches,
            "The original firewall address-list state was restored."
            if matches
            else "Address-list rollback differs from the pre-change baseline.",
        )


class DhcpServerDefinition(RunbookDefinition):
    id = "lan_dhcp"
    title = "LAN DHCP server core"
    command = "/dhcp"
    description = "Create an IP pool and DHCP server on an existing RouterOS DHCP network."
    proposal_tool_name = "propose_lan_dhcp"
    proposal_description = (
        "Propose the harness-owned LAN DHCP core runbook. The DHCP network/gateway entry "
        "must already exist because the pinned backend has no typed tool for creating it."
    )
    fields = (
        RunbookField("name", "DHCP server name", required=True, default="dhcp-lan"),
        RunbookField("interface", "LAN interface", required=True, default="bridge-lan"),
        RunbookField("poolName", "Pool name", required=True, default="pool-lan"),
        RunbookField(
            "ranges",
            "Lease range",
            required=True,
            default="192.168.88.10-192.168.88.254",
            description="One RouterOS IP pool range",
        ),
        RunbookField("leaseTime", "Lease time", default="1d"),
        RunbookField("comment", "Comment", default="Managed by mth"),
        RunbookField(
            "networkConfirmed",
            "DHCP network exists",
            kind=RunbookFieldKind.BOOLEAN,
            default=False,
            description="I verified the matching DHCP network/gateway entry already exists",
            human_only=True,
        ),
    )
    write_tools = frozenset({"manage_ip_pool", "manage_dhcp_server"})
    capability_domains = frozenset({"addressing_services"})
    rollback_note = (
        "Rollback restores the DHCP server and pool snapshots. It does not alter the separate "
        "DHCP network/gateway entry, which this runbook requires to exist beforehand."
    )

    def validate(self, values: Mapping[str, Any]) -> None:
        raw_range = _string(values, "ranges")
        parts = raw_range.split("-", 1)
        if len(parts) != 2:
            raise ValueError("Lease range must look like 192.168.88.10-192.168.88.254")
        try:
            start, end = (ip_address(item.strip()) for item in parts)
        except ValueError as error:
            raise ValueError("Lease range contains an invalid IP address") from error
        if start.version != end.version or int(start) > int(end):
            raise ValueError("Lease range start must not be greater than its end")
        if not _boolean(values, "networkConfirmed"):
            raise ValueError(
                "Confirm that a matching RouterOS DHCP network/gateway entry already exists"
            )

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        server: dict[str, Any] = {
            "action": "add",
            "name": _string(values, "name"),
            "interface": _string(values, "interface"),
            "addressPool": _string(values, "poolName"),
        }
        lease_time = _string(values, "leaseTime")
        comment = _string(values, "comment")
        if lease_time:
            server["leaseTime"] = lease_time
        if comment:
            server["comment"] = comment
        return (
            RunbookStep(
                "manage_ip_pool",
                {
                    "action": "add",
                    "name": _string(values, "poolName"),
                    "ranges": _string(values, "ranges"),
                },
            ),
            RunbookStep("manage_dhcp_server", server),
        )

    def summary(self, values: Mapping[str, Any]) -> str:
        return (
            f'Create pool "{_string(values, "poolName")}" with '
            f'{_string(values, "ranges")}\n'
            f'Create DHCP server "{_string(values, "name")}" on '
            f'{_string(values, "interface")}\n'
            "Requires a pre-existing matching DHCP network/gateway entry."
        )

    async def _current(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        pools = await session.call_tool(
            "list_ip_pools", {"routerId": router_id, "name": _string(values, "poolName")}
        )
        servers = await session.call_tool(
            "list_dhcp_servers", {"routerId": router_id, "limit": 500}
        )
        pool = _matching(_records(pools, "pools"), "name", _string(values, "poolName"))
        server = _matching(
            _records(servers, "servers"), "name", _string(values, "name")
        )
        return pool, server

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        pool, server = await self._current(session, router_id, values)
        return {
            "pool": _project(pool, ("name", "ranges", "next-pool")),
            "server": _project(
                server,
                ("name", "interface", "address-pool", "lease-time", "comment", "disabled"),
            ),
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        pool, server = await self._current(session, router_id, values)
        if pool is None or server is None:
            return RunbookVerification(False, "DHCP pool or server is missing after apply.")
        matches = (
            pool.get("ranges") == _string(values, "ranges")
            and server.get("interface") == _string(values, "interface")
            and server.get("address-pool") == _string(values, "poolName")
        )
        return RunbookVerification(
            matches,
            "DHCP pool and server match the approved plan."
            if matches
            else "DHCP pool or server differs from the approved plan.",
            not _as_bool(server.get("disabled")) if matches else None,
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        pool, server = await self._current(session, router_id, values)
        before_pool = baseline.get("pool")
        before_server = baseline.get("server")
        pool_ok = (before_pool is None and pool is None) or (
            isinstance(before_pool, dict)
            and pool is not None
            and all(pool.get(key) == before_pool.get(key) for key in before_pool)
        )
        server_ok = (before_server is None and server is None) or (
            isinstance(before_server, dict)
            and server is not None
            and all(server.get(key) == before_server.get(key) for key in before_server)
        )
        return RunbookVerification(
            pool_ok and server_ok,
            "Original DHCP pool and server state was restored."
            if pool_ok and server_ok
            else "DHCP rollback differs from the pre-change baseline.",
        )


class DnsResolverDefinition(RunbookDefinition):
    id = "dns_resolver"
    title = "DNS resolver settings"
    command = "/dns"
    description = "Configure RouterOS upstream DNS servers and remote-request policy."
    proposal_tool_name = "propose_dns_resolver"
    proposal_description = (
        "Propose the harness-owned DNS resolver runbook. Supply upstream server IPs and "
        "whether LAN clients may query this router."
    )
    fields = (
        RunbookField(
            "servers",
            "Upstream DNS servers",
            kind=RunbookFieldKind.CSV,
            required=True,
            default=("1.1.1.1", "8.8.8.8"),
        ),
        RunbookField(
            "allowRemoteRequests",
            "Serve LAN DNS queries",
            kind=RunbookFieldKind.BOOLEAN,
            default=False,
        ),
        RunbookField("cacheMaxTtl", "Maximum cache TTL", default="1d"),
    )
    write_tools = frozenset({"manage_dns_settings"})
    capability_domains = frozenset({"addressing_services"})

    def validate(self, values: Mapping[str, Any]) -> None:
        for server in _strings(values, "servers"):
            try:
                ip_address(server)
            except ValueError as error:
                raise ValueError(f"Invalid upstream DNS server: {server}") from error

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        params: dict[str, Any] = {
            "servers": ",".join(_strings(values, "servers")),
            "allowRemoteRequests": _boolean(values, "allowRemoteRequests"),
        }
        ttl = _string(values, "cacheMaxTtl")
        if ttl:
            params["cacheMaxTtl"] = ttl
        return (RunbookStep("manage_dns_settings", params),)

    def summary(self, values: Mapping[str, Any]) -> str:
        remote = "enabled" if _boolean(values, "allowRemoteRequests") else "disabled"
        return (
            f"Set upstream DNS servers: {', '.join(_strings(values, 'servers'))}\n"
            f"Remote DNS requests: {remote}"
        )

    async def _current(self, session: ToolSession, router_id: str) -> dict[str, Any]:
        result = await session.call_tool("get_dns_settings", {"routerId": router_id})
        return _record(result, "settings")

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        del values
        current = await self._current(session, router_id)
        return {
            "settings": _project(
                current,
                ("servers", "allow-remote-requests", "cache-max-ttl"),
            )
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        current = await self._current(session, router_id)
        matches = (
            current.get("servers") == ",".join(_strings(values, "servers"))
            and _as_bool(current.get("allow-remote-requests"))
            == _boolean(values, "allowRemoteRequests")
            and current.get("cache-max-ttl") == _string(values, "cacheMaxTtl")
        )
        return RunbookVerification(
            matches,
            "DNS resolver settings match the approved plan."
            if matches
            else "DNS resolver settings differ from the approved plan.",
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        del values
        current = await self._current(session, router_id)
        before = baseline.get("settings")
        matches = isinstance(before, dict) and all(
            current.get(key) == value for key, value in before.items()
        )
        return RunbookVerification(
            matches,
            "Original DNS resolver settings were restored."
            if matches
            else "DNS resolver rollback differs from the baseline.",
        )


class WireGuardPeerDefinition(RunbookDefinition):
    id = "wireguard_peer"
    title = "WireGuard interface and peer"
    command = "/wireguard"
    description = "Create a RouterOS WireGuard interface and one peer."
    proposal_tool_name = "propose_wireguard_peer"
    proposal_description = (
        "Propose the harness-owned WireGuard interface and peer runbook. RouterOS generates "
        "its private key; provide only the remote peer public key."
    )
    fields = (
        RunbookField("name", "Interface name", required=True, default="wg0"),
        RunbookField("listenPort", "Listen port", default="51820"),
        RunbookField("mtu", "MTU", default="1420"),
        RunbookField("publicKey", "Peer public key", required=True, max_length=64),
        RunbookField("allowedAddress", "Peer allowed address", required=True),
        RunbookField("endpoint", "Peer endpoint", placeholder="optional IP:port"),
        RunbookField("comment", "Comment", default="Managed by mth"),
    )
    write_tools = frozenset({"manage_wireguard_interface", "manage_wireguard_peer"})
    capability_domains = frozenset({"wan_vpn"})

    def validate(self, values: Mapping[str, Any]) -> None:
        for name, minimum, maximum in (
            ("listenPort", 1, 65535),
            ("mtu", 1280, 65535),
        ):
            try:
                value = int(_string(values, name))
            except ValueError as error:
                raise ValueError(f"{name} must be an integer") from error
            if not minimum <= value <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
        public_key = _string(values, "publicKey")
        try:
            decoded = base64.b64decode(public_key, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Peer public key must be valid base64") from error
        if len(decoded) != 32:
            raise ValueError("Peer public key must encode exactly 32 bytes")
        try:
            ip_network(_string(values, "allowedAddress"), strict=False)
        except ValueError as error:
            raise ValueError("Peer allowed address must be a valid IP/CIDR") from error

    def build_steps(
        self,
        values: Mapping[str, Any],
        secrets: Mapping[str, str] | None = None,
    ) -> tuple[RunbookStep, ...]:
        del secrets
        interface: dict[str, Any] = {
            "action": "add",
            "name": _string(values, "name"),
            "listenPort": int(_string(values, "listenPort")),
            "mtu": int(_string(values, "mtu")),
        }
        peer: dict[str, Any] = {
            "action": "add",
            "interface": _string(values, "name"),
            "publicKey": _string(values, "publicKey"),
            "allowedAddress": _string(values, "allowedAddress"),
        }
        endpoint = _string(values, "endpoint")
        comment = _string(values, "comment")
        if comment:
            interface["comment"] = comment
            peer["comment"] = comment
        if endpoint:
            peer["endpoint"] = endpoint
        return (
            RunbookStep("manage_wireguard_interface", interface),
            RunbookStep("manage_wireguard_peer", peer),
        )

    def summary(self, values: Mapping[str, Any]) -> str:
        endpoint = _string(values, "endpoint") or "dynamic"
        return (
            f'Create WireGuard interface "{_string(values, "name")}" on UDP '
            f'{_string(values, "listenPort")}\n'
            f'Add peer for {_string(values, "allowedAddress")} · endpoint {endpoint}\n'
            "RouterOS generates and retains the local private key."
        )

    async def _current(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        interfaces = await session.call_tool(
            "list_wireguard_interfaces", {"routerId": router_id, "limit": 500}
        )
        peers = await session.call_tool(
            "list_wireguard_peers",
            {"routerId": router_id, "interface": _string(values, "name"), "limit": 500},
        )
        interface = _matching(
            _records(interfaces, "interfaces"), "name", _string(values, "name")
        )
        peer = _matching(
            _records(peers, "peers"), "public-key", _string(values, "publicKey")
        )
        return interface, peer

    async def capture_baseline(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        interface, peer = await self._current(session, router_id, values)
        return {
            "interface": _project(
                interface, ("name", "listen-port", "mtu", "disabled", "comment")
            ),
            "peer": _project(
                peer,
                (
                    "interface",
                    "public-key",
                    "allowed-address",
                    "endpoint-address",
                    "comment",
                    "disabled",
                ),
            ),
        }

    async def verify_apply(
        self, session: ToolSession, router_id: str, values: Mapping[str, Any]
    ) -> RunbookVerification:
        interface, peer = await self._current(session, router_id, values)
        if interface is None or peer is None:
            return RunbookVerification(False, "WireGuard interface or peer is missing.")
        matches = (
            str(interface.get("listen-port")) == _string(values, "listenPort")
            and str(interface.get("mtu")) == _string(values, "mtu")
            and peer.get("allowed-address") == _string(values, "allowedAddress")
        )
        return RunbookVerification(
            matches,
            "WireGuard interface and peer match the approved plan."
            if matches
            else "WireGuard interface or peer differs from the approved plan.",
            not _as_bool(interface.get("disabled")) if matches else None,
        )

    async def verify_rollback(
        self,
        session: ToolSession,
        router_id: str,
        values: Mapping[str, Any],
        baseline: Mapping[str, Any],
    ) -> RunbookVerification:
        interface, peer = await self._current(session, router_id, values)
        before_interface = baseline.get("interface")
        before_peer = baseline.get("peer")
        interface_ok = (before_interface is None and interface is None) or (
            isinstance(before_interface, dict)
            and interface is not None
            and all(interface.get(key) == value for key, value in before_interface.items())
        )
        peer_ok = (before_peer is None and peer is None) or (
            isinstance(before_peer, dict)
            and peer is not None
            and all(peer.get(key) == value for key, value in before_peer.items())
        )
        return RunbookVerification(
            interface_ok and peer_ok,
            "Original WireGuard interface and peer state was restored."
            if interface_ok and peer_ok
            else "WireGuard rollback differs from the pre-change baseline.",
        )
