from mth.agent.tool_catalog import CAPABILITY_DOMAINS, ToolCatalogRouter
from mth.core.mcp_client import McpTool


def _tool(name: str) -> McpTool:
    return McpTool(
        name,
        name,
        {"type": "object", "properties": {"routerId": {"type": "string"}}},
        {"readOnlyHint": True, "destructiveHint": False},
    )


def test_capability_packs_route_representative_live_tools_and_proposals() -> None:
    representatives = {
        "overview": "get_system_status",
        "interfaces": "list_bridges",
        "addressing_services": "list_dhcp_servers",
        "firewall_routing": "list_firewall_rules",
        "wan_vpn": "list_pppoe_clients",
        "system": "list_certificates",
        "containers": "get_container_config",
        "diagnostics": "traceroute",
    }
    catalog = tuple(_tool(name) for name in representatives.values())
    router = ToolCatalogRouter()

    assert tuple(representatives) == CAPABILITY_DOMAINS
    for domain, expected in representatives.items():
        names = {tool.name for tool in router.select(catalog, [domain]).tools}
        assert expected in names
        assert router.SELECTOR_NAME in names

    interface_names = {
        tool.name for tool in router.select(catalog, ["interfaces"]).tools
    }
    assert "propose_lan_bridge" in interface_names
    assert "propose_wan_pppoe" not in interface_names

    wan_names = {tool.name for tool in router.select(catalog, ["wan_vpn"]).tools}
    assert {
        "propose_wan_pppoe",
        "propose_wan_nat",
        "propose_wireguard_peer",
    } <= wan_names

    addressing_names = {
        tool.name for tool in router.select(catalog, ["addressing_services"]).tools
    }
    assert {
        "propose_lan_dhcp",
        "propose_dns_resolver",
        "propose_ip_address",
    } <= addressing_names

    ip_read = _tool("list_ip_addresses")
    assert ip_read.name in {
        tool.name for tool in router.select((ip_read,), ["addressing_services"]).tools
    }

    firewall_names = {
        tool.name for tool in router.select(catalog, ["firewall_routing"]).tools
    }
    assert "propose_address_list_entry" in firewall_names


def test_capability_router_rejects_global_write_and_misannotated_tools() -> None:
    router = ToolCatalogRouter()
    router_schema = {
        "type": "object",
        "properties": {"routerId": {"type": "string"}},
    }
    global_schema = {"type": "object", "properties": {"tags": {"type": "array"}}}
    safe = {"readOnlyHint": True, "destructiveHint": False}
    catalog = (
        McpTool("list_interfaces", None, router_schema, safe),
        McpTool("list_routers", None, global_schema, safe),
        McpTool("manage_bridge", None, router_schema, safe),
        McpTool(
            "get_log",
            None,
            router_schema,
            {"readOnlyHint": False, "destructiveHint": False},
        ),
        McpTool(
            "list_files",
            None,
            router_schema,
            {"readOnlyHint": True, "destructiveHint": True},
        ),
    )

    assert tuple(tool.name for tool in router.filter_read_only(catalog)) == (
        "list_interfaces",
    )
    fallback = router.select(catalog, ["unknown"])
    assert fallback.domains == ("overview",)
