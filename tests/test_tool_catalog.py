from mth.agent.tool_catalog import CAPABILITY_DOMAINS, ToolCatalogRouter
from mth.core.mcp_client import McpTool
from mth.core.runbooks import LanBridgeDefinition, RunbookRegistry


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


def test_ready_catalog_exposes_reads_and_only_reviewed_changes_as_proposals() -> None:
    router = ToolCatalogRouter()
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    catalog = (
        McpTool("list_interfaces", None, schema, {"readOnlyHint": True}),
        McpTool("torch", None, schema, {"readOnlyHint": True}),
        McpTool("manage_container", None, schema, {"readOnlyHint": False}),
        McpTool("manage_future", None, schema, {"readOnlyHint": False}),
        McpTool("reboot", None, {"type": "object", "properties": {}}, {"readOnlyHint": False}),
        McpTool("apply_plan", None, schema, {"readOnlyHint": False}),
    )

    assert [tool.name for tool in router.plan_tools(catalog)] == ["list_interfaces", "torch"]
    names = {tool.name for tool in router.ready_tools(catalog)}
    assert {"list_interfaces", "torch"} <= names
    assert "propose_typed_manage_container" not in names
    assert "propose_typed_reboot" not in names
    assert "propose_typed_apply_plan" not in names


def test_high_risk_catalog_keeps_raw_tools_and_adds_persistent_ssh() -> None:
    router = ToolCatalogRouter()
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    catalog = (
        McpTool("list_interfaces", None, schema, {"readOnlyHint": True}),
        McpTool("manage_ip_address", None, schema, {"readOnlyHint": False}),
        McpTool("run_command", None, schema, {"readOnlyHint": False}),
    )

    names = {tool.name for tool in router.high_risk_tools(catalog)}

    assert {"list_interfaces", "manage_ip_address", "run_command", "ssh_exec"} <= names
    assert "propose_typed_manage_ip_address" in names


def test_ready_contract_reports_live_coverage_and_keeps_raw_writes_closed() -> None:
    router = ToolCatalogRouter(RunbookRegistry((LanBridgeDefinition(),)))
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    catalog = (
        McpTool("list_interfaces", None, schema, {"readOnlyHint": True}),
        McpTool("manage_bridge", None, schema, {"readOnlyHint": False}),
        McpTool("manage_bridge_port", None, schema, {"readOnlyHint": False}),
        McpTool("manage_route", None, schema, {"readOnlyHint": False}),
        McpTool("manage_container", None, schema, {"readOnlyHint": False}),
        McpTool("manage_future", None, schema, {"readOnlyHint": False}),
    )

    contract = router.ready_contract(catalog)

    assert contract.plan_reads == contract.ready_reads == ("list_interfaces",)
    assert contract.runbook_writes == ("manage_bridge", "manage_bridge_port")
    assert contract.typed_writes == (
        "manage_bridge",
        "manage_bridge_port",
        "manage_route",
    )
    assert contract.reviewed_exclusions == ("manage_container",)
    assert contract.uncovered_writes == ("manage_future",)
    assert contract.missing_runbook_writes == ()
    assert contract.raw_writes_exposed == ()
    assert contract.safe is True
    assert contract.complete is False
    assert contract.as_dict()["safe"] is True


def test_ready_contract_reports_missing_runbook_dependency() -> None:
    router = ToolCatalogRouter(RunbookRegistry((LanBridgeDefinition(),)))
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    catalog = (
        McpTool("manage_bridge", None, schema, {"readOnlyHint": False}),
    )

    contract = router.ready_contract(catalog)

    assert contract.runbook_writes == ()
    assert contract.missing_runbook_writes == ("manage_bridge_port",)


def test_reviewed_exclusion_completes_classification_without_exposing_raw_write() -> None:
    schema = {"type": "object", "properties": {"routerId": {"type": "string"}}}
    contract = ToolCatalogRouter(RunbookRegistry(())).ready_contract(
        (McpTool("run_command", None, schema, {"readOnlyHint": False}),)
    )

    assert contract.reviewed_exclusions == ("run_command",)
    assert contract.uncovered_writes == ()
    assert contract.raw_writes_exposed == ()
    assert contract.complete is True
