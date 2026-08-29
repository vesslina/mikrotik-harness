from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mth.agent.secret_store import ProviderSecretPaths, ProviderSecretStore
from mth.core.mcp_client.runtime import project_root
from mth.core.registration.models import PendingRegistration
from mth.core.runbooks import DEFAULT_RUNBOOK_REGISTRY


@dataclass(frozen=True, slots=True)
class ConfigPaths:
    root: Path = field(default_factory=lambda: project_root() / ".mth" / "mikromcp")

    @property
    def routers(self) -> Path:
        return self.root / "routers.yaml"

    @property
    def identities(self) -> Path:
        return self.root / "identities.yaml"

    @property
    def dotenv(self) -> Path:
        return self.root / ".env"

    @property
    def router_secrets(self) -> Path:
        return self.root / "router-secrets.json"

    @property
    def router_secrets_key(self) -> Path:
        return self.root / "router-secrets.key"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def audit_log(self) -> Path:
        return self.root / "audit.ndjson"

    @property
    def ssh_hosts(self) -> Path:
        """Private SSH TOFU records, kept separate from MikroMCP's strict schema."""

        return self.root / "ssh-hosts.yaml"


@dataclass(frozen=True, slots=True)
class SshTarget:
    """Private connection details for the dedicated HIGH RISK SSH transport."""

    router_id: str
    host: str
    port: int
    username: str
    password: str = field(repr=False)


class MikroMcpConfigStore:
    OPERATOR_ID = "mth-operator"
    OPERATOR_TOOL_PATTERNS = (
        "list_*",
        "get_*",
        "check_router_health",
        "ping",
        "traceroute",
        "torch",
        "bandwidth_test",
        "plan_changes",
        "apply_plan",
        "rollback_change",
        "manage_*",
        "set_*",
        "create_*",
        "delete_*",
        "export_*",
        "upload_*",
        "fetch_url",
        "reboot",
        "run_script",
        "run_command",
        "write_swos_blob",
        *DEFAULT_RUNBOOK_REGISTRY.write_tools,
    )

    def __init__(
        self,
        paths: ConfigPaths | None = None,
        *,
        router_secret_store: ProviderSecretStore | None = None,
    ) -> None:
        self.paths = paths or ConfigPaths()
        self._router_secrets = router_secret_store or ProviderSecretStore(
            ProviderSecretPaths(
                file=self.paths.router_secrets,
                key_file=self.paths.router_secrets_key,
            )
        )

    def trusted_fingerprint(self, host: str) -> tuple[str, str] | None:
        routers = self._load_yaml(self.paths.routers, "routers")
        for router_id, entry in routers.items():
            if isinstance(entry, dict) and entry.get("host") == host:
                tls = entry.get("tls")
                if isinstance(tls, dict) and isinstance(tls.get("fingerprint"), str):
                    return str(router_id), self._normalize_fingerprint(tls["fingerprint"])
        return None

    def runtime_environment(self) -> dict[str, str]:
        """Load the private child-process environment without exposing it to the UI."""

        self._ensure_operator_policy()
        persisted_environment = self._load_dotenv()
        environment = dict(persisted_environment)
        routers = self._load_yaml(self.paths.routers, "routers")
        self._validate_credential_prefixes(routers)
        for router_id, entry in routers.items():
            if not isinstance(entry, dict):
                continue
            credentials = entry.get("credentials")
            prefix = credentials.get("envPrefix") if isinstance(credentials, dict) else None
            if not isinstance(prefix, str) or not prefix:
                continue
            resolved = self._router_credentials(router_id, prefix, environment)
            if resolved is None:
                continue
            username, password, _was_migrated = resolved
            environment[f"{prefix}_USER"] = username
            environment[f"{prefix}_PASS"] = password
        persisted = dict(environment)
        self._remove_router_credentials_from_environment(persisted, routers)
        if persisted != persisted_environment:
            self._write_dotenv(persisted)
        return environment

    def ssh_target(self, router_id: str) -> SshTarget:
        """Resolve SSH credentials locally without exposing them to UI or model context."""

        routers = self._load_yaml(self.paths.routers, "routers")
        entry = routers.get(router_id)
        if not isinstance(entry, dict):
            raise ValueError(f"Router {router_id!r} is not registered")
        host = entry.get("host")
        credentials = entry.get("credentials")
        if not isinstance(host, str) or not host.strip() or not isinstance(credentials, dict):
            raise ValueError(f"Router {router_id!r} has an invalid SSH configuration")
        prefix = credentials.get("envPrefix")
        if not isinstance(prefix, str) or not prefix:
            raise ValueError(f"Router {router_id!r} has no credential environment prefix")
        environment = self.runtime_environment()
        username = environment.get(f"{prefix}_USER")
        password = environment.get(f"{prefix}_PASS")
        if not username or not password:
            raise ValueError(f"Router {router_id!r} has no saved SSH credentials")
        raw_port = entry.get("sshPort", 22)
        if not isinstance(raw_port, int) or not 1 <= raw_port <= 65535:
            raise ValueError(f"Router {router_id!r} has an invalid SSH port")
        return SshTarget(router_id, host.strip(), raw_port, username, password)

    def ssh_trust(self, router_id: str) -> tuple[str, str] | None:
        """Return canonical public key and SHA-256 hex fingerprint for one router."""

        hosts = self._load_yaml(self.paths.ssh_hosts, "hosts")
        entry = hosts.get(router_id)
        if not isinstance(entry, dict):
            return None
        public_key = entry.get("publicKey")
        fingerprint = entry.get("fingerprint")
        if not isinstance(public_key, str) or not isinstance(fingerprint, str):
            return None
        return public_key, self._normalize_fingerprint(fingerprint)

    def trust_ssh_host(
        self,
        router_id: str,
        *,
        port: int,
        fingerprint: str,
        public_key: str,
    ) -> None:
        """Persist a manually confirmed SSH host key for AsyncSSH and MikroMCP."""

        normalized = self._normalize_fingerprint(fingerprint)
        is_sha256_hex = len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        )
        if not is_sha256_hex:
            raise ValueError("SSH fingerprint must be a SHA-256 hexadecimal digest")
        if not public_key.strip().startswith("ssh-"):
            raise ValueError("SSH public key must be in OpenSSH format")
        routers = self._load_yaml(self.paths.routers, "routers")
        entry = routers.get(router_id)
        if not isinstance(entry, dict):
            raise ValueError(f"Router {router_id!r} is not registered")
        entry["sshPort"] = port
        entry["sshFingerprint"] = normalized
        self._write_yaml(self.paths.routers, {"routers": routers})

        hosts = self._load_yaml(self.paths.ssh_hosts, "hosts")
        hosts[router_id] = {
            "port": port,
            "fingerprint": normalized,
            "publicKey": public_key.strip(),
        }
        self._write_yaml(self.paths.ssh_hosts, {"hosts": hosts})

    def persist(self, pending: PendingRegistration) -> dict[str, str]:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.data.mkdir(parents=True, exist_ok=True)

        routers = self._load_yaml(self.paths.routers, "routers")
        prefix = self._env_prefix(pending.router_id)
        self._validate_credential_prefixes(routers)
        for router_id, entry in routers.items():
            if router_id == pending.router_id:
                continue
            if isinstance(entry, dict) and self._credential_prefix(entry) == prefix:
                raise ValueError(
                    f"Router credential environment prefix collision: {prefix!r}"
                )
        environment = self._load_dotenv()
        self._migrate_legacy_credentials(environment, routers)
        self._remove_router_credentials_from_environment(environment, routers)
        self._router_secrets.set(self._secret_id(pending.router_id, "username"), pending.username)
        self._router_secrets.set(self._secret_id(pending.router_id, "password"), pending.password)
        self._remove_legacy_credentials(environment, prefix)
        routers[pending.router_id] = {
            "host": pending.host,
            "port": pending.port,
            "tls": {
                "enabled": True,
                "rejectUnauthorized": False,
                "fingerprint": pending.tls_fingerprint,
            },
            "credentials": {"source": "env", "envPrefix": prefix},
            "tags": ["mth"],
            "rosVersion": pending.ros_version,
        }
        self._write_yaml(self.paths.routers, {"routers": routers})

        identities = self._load_yaml(self.paths.identities, "identities")
        identities[self.OPERATOR_ID] = {
            "token": "stdio-only-no-bearer-token",
            "role": "operator",
            "allowedRouters": sorted(routers),
            "allowedToolPatterns": list(self.OPERATOR_TOOL_PATTERNS),
        }
        self._write_yaml(self.paths.identities, {"identities": identities})

        environment.setdefault("MIKROMCP_CONFIRMATION_SECRET", secrets.token_hex(32))
        environment.update(
            {
                "MIKROMCP_CONFIG_PATH": str(self.paths.routers.resolve()),
                "MIKROMCP_IDENTITIES_PATH": str(self.paths.identities.resolve()),
                "MIKROMCP_STDIO_IDENTITY": self.OPERATOR_ID,
                "MIKROMCP_DEFAULT_ROUTER": pending.router_id,
                "MIKROMCP_DATA_DIR": str(self.paths.data.resolve()),
                "MIKROMCP_AUDIT_LOG_PATH": str(self.paths.audit_log.resolve()),
                "MIKROMCP_LOG_LEVEL": "warn",
            }
        )
        self._write_dotenv(environment)
        return self.runtime_environment()

    def snapshot_registration_state(self) -> dict[Path, bytes | None]:
        """Capture files changed by registration so failed verification can be undone."""

        paths = (
            self.paths.routers,
            self.paths.identities,
            self.paths.dotenv,
            self.paths.router_secrets,
            self.paths.router_secrets_key,
        )
        return {path: path.read_bytes() if path.is_file() else None for path in paths}

    def restore_registration_state(self, snapshot: Mapping[Path, bytes | None]) -> None:
        for path, content in snapshot.items():
            if content is None:
                with suppress(FileNotFoundError):
                    path.unlink()
            else:
                self._atomic_write_bytes(path, content)

    def _migrate_legacy_credentials(
        self,
        environment: dict[str, str],
        routers: dict[str, Any],
    ) -> None:
        for router_id, entry in routers.items():
            if not isinstance(entry, dict):
                continue
            credentials = entry.get("credentials")
            prefix = credentials.get("envPrefix") if isinstance(credentials, dict) else None
            if isinstance(prefix, str) and prefix:
                self._router_credentials(router_id, prefix, environment)

    @classmethod
    def _remove_router_credentials_from_environment(
        cls,
        environment: dict[str, str],
        routers: Mapping[str, Any],
    ) -> None:
        for entry in routers.values():
            if not isinstance(entry, dict):
                continue
            credentials = entry.get("credentials")
            prefix = credentials.get("envPrefix") if isinstance(credentials, dict) else None
            if isinstance(prefix, str) and prefix:
                cls._remove_legacy_credentials(environment, prefix)

    def _router_credentials(
        self,
        router_id: str,
        prefix: str,
        environment: Mapping[str, str],
    ) -> tuple[str, str, bool] | None:
        username = self._router_secrets.get(self._secret_id(router_id, "username"))
        password = self._router_secrets.get(self._secret_id(router_id, "password"))
        if username is not None and password is not None:
            had_legacy = (
                f"{prefix}_USER" in environment or f"{prefix}_PASS" in environment
            )
            self._remove_legacy_credentials(environment, prefix)
            return username, password, had_legacy

        legacy_username = environment.get(f"{prefix}_USER")
        legacy_password = environment.get(f"{prefix}_PASS")
        if legacy_username and legacy_password:
            self._router_secrets.set(self._secret_id(router_id, "username"), legacy_username)
            self._router_secrets.set(self._secret_id(router_id, "password"), legacy_password)
            self._remove_legacy_credentials(environment, prefix)
            return legacy_username, legacy_password, True
        return None

    @staticmethod
    def _remove_legacy_credentials(environment: Mapping[str, str], prefix: str) -> None:
        if isinstance(environment, dict):
            environment.pop(f"{prefix}_USER", None)
            environment.pop(f"{prefix}_PASS", None)

    @staticmethod
    def _secret_id(router_id: str, kind: str) -> str:
        return f"router:{router_id}:{kind}"

    @classmethod
    def _validate_credential_prefixes(cls, routers: Mapping[str, Any]) -> None:
        owners: dict[str, str] = {}
        for router_id, entry in routers.items():
            if not isinstance(entry, dict):
                continue
            prefix = cls._credential_prefix(entry)
            if prefix is None:
                continue
            owner = owners.setdefault(prefix, router_id)
            if owner != router_id:
                raise ValueError(
                    f"Router credential environment prefix collision: {prefix!r}"
                )

    @staticmethod
    def _credential_prefix(entry: Mapping[str, Any]) -> str | None:
        credentials = entry.get("credentials")
        prefix = credentials.get("envPrefix") if isinstance(credentials, dict) else None
        return prefix if isinstance(prefix, str) and prefix else None

    def _ensure_operator_policy(self) -> None:
        """Migrate an existing local identity to the reviewed runbook allowlist."""

        if not self.paths.identities.exists():
            return
        identities = self._load_yaml(self.paths.identities, "identities")
        operator = identities.get(self.OPERATOR_ID)
        if not isinstance(operator, dict):
            return
        expected = list(self.OPERATOR_TOOL_PATTERNS)
        if operator.get("allowedToolPatterns") == expected:
            return
        operator["allowedToolPatterns"] = expected
        self._write_yaml(self.paths.identities, {"identities": identities})

    def _load_dotenv(self) -> dict[str, str]:
        if not self.paths.dotenv.exists():
            return {}
        parsed: dict[str, str] = {}
        for line in self.paths.dotenv.read_text(encoding="utf-8").splitlines():
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            if isinstance(value, str):
                parsed[key] = value
        return parsed

    def _write_dotenv(self, env: dict[str, str]) -> None:
        content = "".join(f"{key}={json.dumps(value)}\n" for key, value in sorted(env.items()))
        self._atomic_write(self.paths.dotenv, content)
        with suppress(OSError):
            self.paths.dotenv.chmod(0o600)

    @staticmethod
    def _load_yaml(path: Path, root_key: str) -> dict[str, Any]:
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid YAML document: {path}")
        entries = loaded.get(root_key, {})
        if not isinstance(entries, dict):
            raise ValueError(f"Invalid {root_key} mapping: {path}")
        return dict(entries)

    def _write_yaml(self, path: Path, data: dict[str, Any]) -> None:
        content = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
        self._atomic_write(path, content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    @staticmethod
    def _env_prefix(router_id: str) -> str:
        return "ROUTER_" + "".join(
            character.upper() if character.isalnum() else "_" for character in router_id
        )

    @staticmethod
    def _normalize_fingerprint(value: str) -> str:
        return value.replace(":", "").lower()
