from __future__ import annotations

import json
import os
import secrets
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mth.core.mcp_client.runtime import project_root
from mth.core.registration.models import PendingRegistration
from mth.core.runbooks import APPROVED_TYPED_CHANGE_DOMAINS, DEFAULT_RUNBOOK_REGISTRY


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
    def data(self) -> Path:
        return self.root / "data"

    @property
    def audit_log(self) -> Path:
        return self.root / "audit.ndjson"


class MikroMcpConfigStore:
    OPERATOR_ID = "mth-operator"
    OPERATOR_TOOL_PATTERNS = (
        "list_*",
        "get_*",
        "check_router_health",
        "ping",
        "traceroute",
        "plan_changes",
        "apply_plan",
        "rollback_change",
        *DEFAULT_RUNBOOK_REGISTRY.write_tools,
        *sorted(APPROVED_TYPED_CHANGE_DOMAINS),
    )

    def __init__(self, paths: ConfigPaths | None = None) -> None:
        self.paths = paths or ConfigPaths()

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
        return self._load_dotenv()

    def persist(self, pending: PendingRegistration) -> dict[str, str]:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.paths.data.mkdir(parents=True, exist_ok=True)

        routers = self._load_yaml(self.paths.routers, "routers")
        prefix = self._env_prefix(pending.router_id)
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

        env = self._load_dotenv()
        env[f"{prefix}_USER"] = pending.username
        env[f"{prefix}_PASS"] = pending.password
        env.setdefault("MIKROMCP_CONFIRMATION_SECRET", secrets.token_hex(32))
        env.update(
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
        self._write_dotenv(env)
        return env

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
    def _env_prefix(router_id: str) -> str:
        return "ROUTER_" + "".join(
            character.upper() if character.isalnum() else "_" for character in router_id
        )

    @staticmethod
    def _normalize_fingerprint(value: str) -> str:
        return value.replace(":", "").lower()
