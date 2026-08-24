"""Narrow, read-only RouterOS REST extension for gaps in pinned MikroMCP.

MikroMCP 1.10 has a typed ``manage_ip_address`` change tool but does not expose a
corresponding list tool.  The harness keeps writes inside MikroMCP; this module
only supplies that missing inspection primitive and verifies the same TOFU TLS
fingerprint stored during registration before every request.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class RouterOsRestReadError(RuntimeError):
    """A safe-to-display failure from the narrow RouterOS REST reader."""


@dataclass(frozen=True, slots=True)
class RouterRestTarget:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    fingerprint: str


class RouterOsRestReader:
    """Read RouterOS address records without adding a second write path."""

    def __init__(self, environment: Mapping[str, str], *, timeout: float = 10.0) -> None:
        self._environment = dict(environment)
        self._timeout = timeout

    def list_ip_addresses(
        self,
        router_id: str,
        *,
        interface: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise RouterOsRestReadError("The IP address list limit must be between 1 and 500.")
        target = self._target(router_id)
        records = self._get_json(target, "/rest/ip/address")
        if not isinstance(records, list):
            raise RouterOsRestReadError("RouterOS returned an invalid IP address list response.")
        projected: list[dict[str, Any]] = []
        for raw in records:
            if not isinstance(raw, dict):
                continue
            address = raw.get("address")
            bound_interface = raw.get("interface")
            if not isinstance(address, str) or not isinstance(bound_interface, str):
                continue
            if interface is not None and bound_interface != interface:
                continue
            projected.append(
                {
                    "address": address,
                    "interface": bound_interface,
                    "network": self._text(raw.get("network")),
                    "comment": self._text(raw.get("comment")),
                    "disabled": self._truth(raw.get("disabled")),
                    "dynamic": self._truth(raw.get("dynamic")),
                    "invalid": self._truth(raw.get("invalid")),
                }
            )
            if len(projected) >= limit:
                break
        return projected

    def _target(self, router_id: str) -> RouterRestTarget:
        path = self._environment.get("MIKROMCP_CONFIG_PATH")
        if not path:
            raise RouterOsRestReadError("MikroMCP router configuration is unavailable.")
        try:
            parsed = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise RouterOsRestReadError(
                "Could not read the trusted router configuration."
            ) from error
        routers = parsed.get("routers") if isinstance(parsed, dict) else None
        config = routers.get(router_id) if isinstance(routers, dict) else None
        if not isinstance(config, dict):
            raise RouterOsRestReadError(f"Router {router_id!r} is not registered.")
        host = config.get("host")
        port = config.get("port", 443)
        credentials = config.get("credentials")
        tls = config.get("tls")
        if not isinstance(host, str) or not host or not isinstance(port, int):
            raise RouterOsRestReadError("Registered router endpoint is invalid.")
        prefix = credentials.get("envPrefix") if isinstance(credentials, dict) else None
        fingerprint = tls.get("fingerprint") if isinstance(tls, dict) else None
        if not isinstance(prefix, str) or not isinstance(fingerprint, str) or not fingerprint:
            raise RouterOsRestReadError(
                "Registered router credentials or TLS fingerprint are missing."
            )
        username = self._environment.get(f"{prefix}_USER")
        password = self._environment.get(f"{prefix}_PASS")
        if not username or password is None:
            raise RouterOsRestReadError("Registered RouterOS credentials are unavailable.")
        return RouterRestTarget(
            host=host,
            port=port,
            username=username,
            password=password,
            fingerprint=fingerprint.replace(":", "").casefold(),
        )

    def _get_json(self, target: RouterRestTarget, path: str) -> Any:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            target.host,
            target.port,
            timeout=self._timeout,
            context=context,
        )
        try:
            connection.connect()
            socket = connection.sock
            certificate = socket.getpeercert(binary_form=True) if socket is not None else None
            actual = hashlib.sha256(certificate or b"").hexdigest()
            if not certificate or actual.casefold() != target.fingerprint:
                raise RouterOsRestReadError("Router TLS fingerprint changed; the read was blocked.")
            credentials = f"{target.username}:{target.password}".encode()
            authorization = base64.b64encode(credentials).decode("ascii")
            connection.request(
                "GET",
                path,
                headers={"Accept": "application/json", "Authorization": f"Basic {authorization}"},
            )
            response = connection.getresponse()
            body = response.read()
            if response.status >= 300:
                raise RouterOsRestReadError(
                    f"RouterOS refused the IP address read (HTTP {response.status})."
                )
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RouterOsRestReadError(
                    "RouterOS returned malformed JSON for IP addresses."
                ) from error
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            raise RouterOsRestReadError(
                f"Could not read IP addresses from RouterOS: {error}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _truth(value: object) -> bool:
        return value is True or (isinstance(value, str) and value.casefold() == "true")
