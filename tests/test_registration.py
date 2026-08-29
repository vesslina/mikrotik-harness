import asyncio
import ssl

import pytest
import yaml

from mth.agent.secret_store import ProviderSecretPaths, ProviderSecretStore
from mth.core.discovery.models import DeviceInfo
from mth.core.mcp_client.models import BackendInspection, McpTool, McpToolResult
from mth.core.registration import (
    ConfigPaths,
    MikroMcpConfigStore,
    PendingRegistration,
    RegistrationError,
    RegistrationErrorCode,
    RegistrationService,
    capture_tls_fingerprint,
)


class _Protector:
    name = "test-protector"

    def protect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0xA5 for byte in value)

    def unprotect(self, value: bytes) -> bytes:
        return bytes(byte ^ 0xA5 for byte in value)


def _store(tmp_path) -> MikroMcpConfigStore:
    return MikroMcpConfigStore(
        ConfigPaths(root=tmp_path),
        router_secret_store=ProviderSecretStore(
            ProviderSecretPaths(
                file=tmp_path / "router-secrets.json",
                key_file=tmp_path / "router-secrets.key",
            ),
            protector=_Protector(),
        ),
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


def test_tls_handshake_failure_identifies_wrong_routeros_service(monkeypatch) -> None:
    def fail_connection(*_args, **_kwargs):
        raise ssl.SSLError("handshake failure")

    monkeypatch.setattr(
        "mth.core.registration.service.socket.create_connection",
        fail_connection,
    )

    with pytest.raises(RegistrationError) as raised:
        capture_tls_fingerprint("192.168.56.103")

    assert raised.value.code == RegistrationErrorCode.REST_API_DISABLED
    assert "www-ssl, not api-ssl" in str(raised.value)


def test_refused_tls_connection_recommends_www_ssl(monkeypatch) -> None:
    def refuse_connection(*_args, **_kwargs):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(
        "mth.core.registration.service.socket.create_connection",
        refuse_connection,
    )

    with pytest.raises(RegistrationError) as raised:
        capture_tls_fingerprint("192.168.56.103")

    assert raised.value.code == RegistrationErrorCode.REST_API_DISABLED
    assert "HTTPS REST service www-ssl" in str(raised.value)


def test_store_writes_pinned_router_operator_identity_and_separate_secret(tmp_path) -> None:
    store = _store(tmp_path)
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
    assert "manage_pppoe_client" in operator["allowedToolPatterns"]
    assert "manage_bridge" in operator["allowedToolPatterns"]
    assert "manage_bridge_port" in operator["allowedToolPatterns"]
    assert "manage_firewall_rule" in operator["allowedToolPatterns"]
    assert "manage_ip_service" in operator["allowedToolPatterns"]
    assert "manage_ip_pool" in operator["allowedToolPatterns"]
    assert "manage_ip_address" in operator["allowedToolPatterns"]
    assert "manage_address_list_entry" in operator["allowedToolPatterns"]
    assert "manage_dhcp_server" in operator["allowedToolPatterns"]
    assert "manage_dns_settings" in operator["allowedToolPatterns"]
    assert "manage_wireguard_interface" in operator["allowedToolPatterns"]
    assert "manage_wireguard_peer" in operator["allowedToolPatterns"]
    assert "apply_plan" in operator["allowedToolPatterns"]
    assert "run_command" in operator["allowedToolPatterns"]
    assert "manage_*" in operator["allowedToolPatterns"]
    assert environment["ROUTER_MIKROTIK_AFE23E_PASS"] == "top-secret"
    assert "top-secret" not in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "top-secret" not in (tmp_path / "router-secrets.json").read_text(encoding="utf-8")


def test_store_resolves_private_ssh_target_and_persists_separate_ssh_tofu(tmp_path) -> None:
    store = _store(tmp_path)
    store.persist(
        PendingRegistration(
            router_id="mikrotik-afe23e",
            host="192.168.56.103",
            port=443,
            username="admin",
            password="router-password",
            ros_version="7.21.5",
            tls_fingerprint="ab" * 32,
        )
    )

    target = store.ssh_target("mikrotik-afe23e")
    assert (target.host, target.port, target.username, target.password) == (
        "192.168.56.103",
        22,
        "admin",
        "router-password",
    )

    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestHarnessHostKey"
    store.trust_ssh_host(
        "mikrotik-afe23e",
        port=22,
        fingerprint="cd" * 32,
        public_key=public_key,
    )

    assert store.ssh_trust("mikrotik-afe23e") == (public_key, "cd" * 32)
    routers = yaml.safe_load((tmp_path / "routers.yaml").read_text(encoding="utf-8"))
    assert routers["routers"]["mikrotik-afe23e"]["sshFingerprint"] == "cd" * 32
    assert "router-password" not in (tmp_path / "ssh-hosts.yaml").read_text(encoding="utf-8")


def test_runtime_environment_migrates_legacy_router_credentials(tmp_path) -> None:
    store = _store(tmp_path)
    (tmp_path / "routers.yaml").write_text(
        """routers:
  mikrotik-afe23e:
    host: 192.168.56.103
    port: 443
    tls:
      fingerprint: abababababababababababababababababababababababababababababababab
    credentials:
      source: env
      envPrefix: ROUTER_MIKROTIK_AFE23E
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        'ROUTER_MIKROTIK_AFE23E_USER="admin"\n'
        'ROUTER_MIKROTIK_AFE23E_PASS="legacy-secret"\n',
        encoding="utf-8",
    )

    environment = store.runtime_environment()

    assert environment["ROUTER_MIKROTIK_AFE23E_USER"] == "admin"
    assert environment["ROUTER_MIKROTIK_AFE23E_PASS"] == "legacy-secret"
    assert "legacy-secret" not in (tmp_path / ".env").read_text(encoding="utf-8")
    assert store.ssh_target("mikrotik-afe23e").password == "legacy-secret"


def test_runtime_environment_rejects_credential_prefix_collisions(tmp_path) -> None:
    store = _store(tmp_path)
    (tmp_path / "routers.yaml").write_text(
        """routers:
  first:
    host: 192.0.2.1
    credentials: {envPrefix: ROUTER_SHARED}
  second:
    host: 192.0.2.2
    credentials: {envPrefix: ROUTER_SHARED}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="prefix collision"):
        store.runtime_environment()


def test_prepare_rejects_routeros_6_before_network_access(tmp_path) -> None:
    called = False

    def capture(_host: str, _port: int, _timeout: float) -> str:
        nonlocal called
        called = True
        return "ab" * 32

    service = RegistrationService(
        store=_store(tmp_path),
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
    store = _store(tmp_path)
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
        store=_store(tmp_path),
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


def test_registration_restores_files_when_backend_health_fails(tmp_path) -> None:
    store = _store(tmp_path)
    (tmp_path / "routers.yaml").write_text("routers: {}\n", encoding="utf-8")
    (tmp_path / "identities.yaml").write_text("identities: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text('KEEP="yes"\n', encoding="utf-8")
    before = {
        path.name: path.read_bytes()
        for path in (tmp_path / "routers.yaml", tmp_path / "identities.yaml", tmp_path / ".env")
    }

    class FailingClient:
        async def inspect_router(self, _router_id: str) -> BackendInspection:
            return BackendInspection(
                tools=(),
                health=McpToolResult(("unhealthy",), {"healthy": False}, True),
                system_status=McpToolResult((), {}, False),
            )

    service = RegistrationService(
        store=store,
        client_factory=lambda _environment: FailingClient(),
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

    with pytest.raises(RegistrationError) as raised:
        asyncio.run(service.register_and_verify(pending))

    assert raised.value.code == RegistrationErrorCode.BACKEND_HEALTH_FAILED
    assert {
        path.name: path.read_bytes()
        for path in (tmp_path / "routers.yaml", tmp_path / "identities.yaml", tmp_path / ".env")
    } == before
    assert not (tmp_path / "router-secrets.json").exists()


def test_registration_restores_files_when_verification_is_cancelled(tmp_path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        (tmp_path / "routers.yaml").write_text("routers: {}\n", encoding="utf-8")
        before = (tmp_path / "routers.yaml").read_bytes()
        started = asyncio.Event()

        class HangingClient:
            async def inspect_router(self, _router_id: str) -> BackendInspection:
                started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

        service = RegistrationService(
            store=store,
            client_factory=lambda _environment: HangingClient(),
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
        task = asyncio.create_task(service.register_and_verify(pending))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "routers.yaml").read_bytes() == before
        assert not (tmp_path / "router-secrets.json").exists()

    asyncio.run(scenario())
