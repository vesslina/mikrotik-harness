from __future__ import annotations

from mth.core.mcp_client import MikroMcpClient
from mth.core.mcp_client.rest_reader import RouterOsRestReader


def test_harness_adds_missing_read_only_ip_address_tool() -> None:
    tools = MikroMcpClient._augment_read_catalog(())

    assert tuple(tool.name for tool in tools) == ("list_ip_addresses",)
    assert tools[0].annotations == {"readOnlyHint": True, "destructiveHint": False}


def test_rest_reader_resolves_only_the_registered_router(tmp_path) -> None:
    config = tmp_path / "routers.yaml"
    config.write_text(
        """routers:
  router-one:
    host: 192.0.2.1
    port: 443
    tls:
      fingerprint: abcd
    credentials:
      envPrefix: ROUTER_ONE
""",
        encoding="utf-8",
    )
    reader = RouterOsRestReader(
        {
            "MIKROMCP_CONFIG_PATH": str(config),
            "ROUTER_ONE_USER": "operator",
            "ROUTER_ONE_PASS": "not-printed",
        }
    )

    target = reader._target("router-one")

    assert (target.host, target.port, target.username, target.fingerprint) == (
        "192.0.2.1",
        443,
        "operator",
        "abcd",
    )
    assert "not-printed" not in repr(target)
