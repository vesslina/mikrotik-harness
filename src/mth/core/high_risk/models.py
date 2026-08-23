from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class HighRiskError(RuntimeError):
    """A HIGH RISK pre-flight or persistent-session failure."""


@dataclass(frozen=True, slots=True)
class SshHostKey:
    router_id: str
    host: str
    port: int
    algorithm: str
    fingerprint: str
    raw_fingerprint: str
    public_key: str


class SshTrustRequired(HighRiskError):
    """First use of a host key requires a person to confirm its fingerprint."""

    def __init__(self, host_key: SshHostKey) -> None:
        self.host_key = host_key
        super().__init__(
            f"SSH host key for {host_key.host}:{host_key.port} needs confirmation: "
            f"{host_key.fingerprint}"
        )


class HostKeyMismatchError(HighRiskError):
    """A previously pinned host key changed; never offer automatic replacement."""


@dataclass(frozen=True, slots=True)
class HighRiskArtifacts:
    router_id: str
    created_at: str
    backup_path: Path
    export_path: Path
    manifest_path: Path
    backup_remote_name: str
    export_remote_name: str
    backup_secret_id: str


@dataclass(frozen=True, slots=True)
class SshExecResult:
    raw_output: str
    cleaned_output: str
    status: str
    session_alive: bool
    safe_mode_active: bool
    execution_time: float
    output_truncated: bool
    command_count: int
    error_detail: str | None = None
