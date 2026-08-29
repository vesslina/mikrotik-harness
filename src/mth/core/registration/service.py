from __future__ import annotations

import asyncio
import hashlib
import re
import socket
import ssl
from collections.abc import Callable
from typing import Protocol

from mth.core.discovery.models import DeviceInfo
from mth.core.mcp_client import BackendInspection, MikroMcpClient, RuntimeUnavailableError
from mth.core.registration.errors import RegistrationError, RegistrationErrorCode
from mth.core.registration.models import PendingRegistration, RegistrationResult
from mth.core.registration.store import MikroMcpConfigStore


class BackendClient(Protocol):
    async def inspect_router(self, router_id: str) -> BackendInspection: ...


ClientFactory = Callable[[dict[str, str]], BackendClient]
FingerprintCapture = Callable[[str, int, float], str]


def capture_tls_fingerprint(host: str, port: int = 443, timeout: float = 4.0) -> str:
    """Capture the leaf certificate only; MikroMCP remains the pin enforcer."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with (
            socket.create_connection((host, port), timeout=timeout) as raw_socket,
            context.wrap_socket(raw_socket, server_hostname=host) as tls_socket,
        ):
            certificate = tls_socket.getpeercert(binary_form=True)
    except ssl.SSLError as error:
        raise RegistrationError(
            RegistrationErrorCode.REST_API_DISABLED,
            (
                f"TLS handshake failed at {host}:{port}. RouterOS REST is served by "
                f"www-ssl, not api-ssl. Configure www-ssl with a certificate: {error}"
            ),
        ) from error
    except (TimeoutError, ConnectionError, OSError) as error:
        raise RegistrationError(
            RegistrationErrorCode.REST_API_DISABLED,
            (
                f"RouterOS HTTPS REST service www-ssl is not reachable at "
                f"{host}:{port}. Enable www-ssl with a certificate: {error}"
            ),
        ) from error
    if not certificate:
        raise RegistrationError(
            RegistrationErrorCode.REST_API_DISABLED,
            f"RouterOS www-ssl at {host}:{port} did not present a TLS certificate.",
        )
    return hashlib.sha256(certificate).hexdigest()


class RegistrationService:
    def __init__(
        self,
        *,
        store: MikroMcpConfigStore | None = None,
        capture_fingerprint: FingerprintCapture = capture_tls_fingerprint,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.store = store or MikroMcpConfigStore()
        self._capture_fingerprint = capture_fingerprint
        self._client_factory = client_factory or (
            lambda environment: MikroMcpClient(environment=environment)
        )

    def prepare(
        self,
        *,
        host: str,
        username: str,
        password: str,
        device: DeviceInfo | None = None,
        port: int = 443,
    ) -> PendingRegistration:
        host = host.strip()
        if not host or "://" in host or "/" in host:
            raise RegistrationError(
                RegistrationErrorCode.INVALID_TARGET,
                "Enter an IP address or hostname without a URL scheme or path.",
            )
        if not password:
            raise RegistrationError(
                RegistrationErrorCode.INVALID_TARGET,
                "MikroMCP requires a non-empty RouterOS password.",
            )

        ros_version = self._ros_version(device)
        fingerprint = self._capture_fingerprint(host, port, 4.0).replace(":", "").lower()
        trusted = self.store.trusted_fingerprint(host)
        if trusted is not None and trusted[1] != fingerprint:
            raise RegistrationError(
                RegistrationErrorCode.TLS_FINGERPRINT_MISMATCH,
                f"TLS fingerprint mismatch for {host}. Expected {trusted[1]}, got {fingerprint}.",
            )

        router_id = trusted[0] if trusted else self._router_id(host, device)
        return PendingRegistration(
            router_id=router_id,
            host=host,
            port=port,
            username=username,
            password=password,
            ros_version=ros_version,
            tls_fingerprint=fingerprint,
            identity=device.identity if device else None,
            trusted_fingerprint=trusted is not None,
        )

    async def register_and_verify(self, pending: PendingRegistration) -> RegistrationResult:
        snapshot = self.store.snapshot_registration_state()
        try:
            environment = self.store.persist(pending)
        except Exception:
            self.store.restore_registration_state(snapshot)
            raise
        try:
            client = self._client_factory(environment)
            inspection = await client.inspect_router(pending.router_id)
            health = inspection.health.structured_content or {}
            if not isinstance(health, dict):
                raise RegistrationError(
                    RegistrationErrorCode.BACKEND_HEALTH_FAILED,
                    "MikroMCP returned malformed health data",
                )
            if inspection.health.is_error or health.get("healthy") is not True:
                detail = inspection.health.text or str(
                    health.get("error", "unknown health failure")
                )
                raise RegistrationError(RegistrationErrorCode.BACKEND_HEALTH_FAILED, detail)
            status = inspection.system_status.structured_content or {}
            if not isinstance(status, dict):
                raise RegistrationError(
                    RegistrationErrorCode.BACKEND_HEALTH_FAILED,
                    "MikroMCP returned malformed system status data",
                )
            if inspection.system_status.is_error:
                raise RegistrationError(
                    RegistrationErrorCode.BACKEND_HEALTH_FAILED,
                    inspection.system_status.text or "get_system_status failed",
                )

            sections = status.get("sections", {})
            identity_section = sections.get("identity", {}) if isinstance(sections, dict) else {}
            identity = str(
                identity_section.get("name")
                if isinstance(identity_section, dict)
                else pending.identity or pending.router_id
            )
            if not identity or identity == "None":
                identity = pending.identity or pending.router_id
            return RegistrationResult(
                router_id=pending.router_id,
                identity=identity,
                tool_count=len(inspection.tools),
                health=health,
                system_status=status,
            )
        except asyncio.CancelledError:
            self.store.restore_registration_state(snapshot)
            raise
        except RegistrationError:
            self.store.restore_registration_state(snapshot)
            raise
        except RuntimeUnavailableError as error:
            self.store.restore_registration_state(snapshot)
            raise RegistrationError(
                RegistrationErrorCode.BACKEND_UNAVAILABLE,
                str(error),
            ) from error
        except Exception as error:
            self.store.restore_registration_state(snapshot)
            raise RegistrationError(
                RegistrationErrorCode.BACKEND_HEALTH_FAILED,
                f"MikroMCP connection failed: {error}",
            ) from error

    @staticmethod
    def _ros_version(device: DeviceInfo | None) -> str:
        version = device.version if device else None
        if not version:
            return "7"
        match = re.search(r"(?<!\d)(\d+(?:\.\d+){0,3})", version)
        if not match:
            return "7"
        parsed = match.group(1)
        if int(parsed.split(".", 1)[0]) < 7:
            raise RegistrationError(
                RegistrationErrorCode.ROUTEROS_VERSION_UNSUPPORTED,
                f"MikroMCP requires RouterOS 7.x; discovered version is {version}.",
            )
        return parsed

    @staticmethod
    def _router_id(host: str, device: DeviceInfo | None) -> str:
        base = (device.identity if device and device.identity else "router").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "router"
        suffix_source = device.mac if device and device.mac else host
        suffix = re.sub(r"[^a-zA-Z0-9]", "", suffix_source)[-6:].lower()
        return f"{slug}-{suffix}" if suffix else slug
