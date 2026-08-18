import asyncio

import pytest
import yaml

from mth.core.discovery.models import DeviceInfo
from mth.core.mcp_client.models import BackendInspection, McpTool, McpToolResult
from mth.core.registration import (
    ConfigPaths,
    MikroMcpConfigStore,
    PendingRegistration,
    RegistrationError,
    RegistrationErrorCode,
    RegistrationService,
)


def _device(version: str = "7.21.5 (long-term)") -> DeviceInfo:
    return DeviceInfo(
        mac="08:00:27:AF:E2:3E",
        ipv4_addresses=("192.168.56.103",),
        identity="MikroTik",
        version=version,
        board="CHR",
        source_ip="192.168.56.103",
    )


def test_store_writes_pinned_router_operator_identity_and_separate_secret(tmp_path) -> None:
    store = MikroMcpConfigStore(ConfigPaths(root=tmp_path))
    pending = PendingRegistration(
        router_id="mikrotik-afe23e",
        host="192.168.56.103",
        port=443,
        username="mcp-api",
        password="top-secret",
        ros_version="7.21.5",
        tls_fingerprint="ab" * 32,
    )

    environment = store.persist(pending)

    routers = yaml.safe_load((tmp_path / "routers.yaml").read_text(encoding="utf-8"))
    router = routers["routers"][pending.router_id]
    assert router["tls"]["fingerprint"] == "ab" * 32
    assert router["tls"]["rejectUnauthorized"] is False
    assert "top-secret" not in (tmp_path / "routers.yaml").read_text(encoding="utf-8")

    identities = yaml.safe_load((tmp_path / "identities.yaml").read_text(encoding="utf-8"))
    operator = identities["identities"]["mth-operator"]
    assert operator["role"] == "operator"
    assert operator["allowedRouters"] == [pending.router_id]
    assert "check_router_health" in operator["allowedToolPatterns"]
    assert environment["ROUTER_MIKROTIK_AFE23E_PASS"] == "top-secret"
    assert "top-secret" in (tmp_path / ".env").read_text(encoding="utf-8")


def test_prepare_rejects_routeros_6_before_network_access(tmp_path) -> None:
    called = False

    def capture(_host: str, _port: int, _timeout: float) -> str:
        nonlocal called
        called = True
        return "ab" * 32

    service = RegistrationService(
        store=MikroMcpConfigStore(ConfigPaths(root=tmp_path)),
        capture_fingerprint=capture,
    )

    with pytest.raises(RegistrationError) as raised:
        service.prepare(
            host="192.168.56.103",
            username="admin",
            password="secret",
            device=_device("6.49.17"),
        )

    assert raised.value.code == RegistrationErrorCode.ROUTEROS_VERSION_UNSUPPORTED
    assert called is False


def test_prepare_hard_stops_on_changed_tls_fingerprint(tmp_path) -> None:
    store = MikroMcpConfigStore(ConfigPaths(root=tmp_path))
    store.persist(
        PendingRegistration(
            router_id="mikrotik-afe23e",
            host="192.168.56.103",
            port=443,
            username="admin",
            password="secret",
            ros_version="7.21.5",
            tls_fingerprint="aa" * 32,
        )
    )
    service = RegistrationService(
        store=store,
        capture_fingerprint=lambda _host, _port, _timeout: "bb" * 32,
    )

    with pytest.raises(RegistrationError) as raised:
        service.prepare(
            host="192.168.56.103",
            username="admin",
            password="secret",
            device=_device(),
        )

    assert raised.value.code == RegistrationErrorCode.TLS_FINGERPRINT_MISMATCH


def test_registration_uses_live_catalog_health_and_system_status(tmp_path) -> None:
    inspection = BackendInspection(
        tools=(
            McpTool("check_router_health", None, {}),
            McpTool("get_system_status", None, {}),
            McpTool("list_interfaces", None, {}),
        ),
        health=McpToolResult((), {"healthy": True, "rosVersion": "7.21.5"}, False),
        system_status=McpToolResult(
            (),
            {
                "sections": {
                    "identity": {"name": "MikroTik"},
                    "resource": {"cpu-load": "2"},
                }
            },
            False,
        ),
    )

    class FakeClient:
        async def inspect_router(self, router_id: str) -> BackendInspection:
            assert router_id == "mikrotik-afe23e"
            return inspection

    service = RegistrationService(
        store=MikroMcpConfigStore(ConfigPaths(root=tmp_path)),
        client_factory=lambda _environment: FakeClient(),
    )
    pending = PendingRegistration(
        router_id="mikrotik-afe23e",
        host="192.168.56.103",
        port=443,
        username="admin",
        password="secret",
        ros_version="7.21.5",
        tls_fingerprint="ab" * 32,
    )

    result = asyncio.run(service.register_and_verify(pending))

    assert result.identity == "MikroTik"
    assert result.tool_count == 3
    assert result.health["healthy"] is True
