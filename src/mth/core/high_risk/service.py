from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import asyncssh

from mth.agent.secret_store import ProviderSecretPaths, ProviderSecretStore
from mth.core.high_risk.models import (
    HighRiskArtifacts,
    HighRiskError,
    HostKeyMismatchError,
    SshHostKey,
    SshTrustRequired,
)
from mth.core.high_risk.ssh import RouterOsSshSession
from mth.core.mcp_client import MikroMcpClient
from mth.core.mcp_client.models import McpToolResult
from mth.core.registration import MikroMcpConfigStore, SshTarget


class BackupBackend(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> McpToolResult: ...


ConnectionFactory = Callable[[SshTarget, Path], Awaitable[RouterOsSshSession]]


class HighRiskSession:
    """A UI-owned elevated session, deliberately not serialised into chat/model history."""

    def __init__(
        self,
        ssh: RouterOsSshSession,
        artifacts: HighRiskArtifacts,
        secrets_store: ProviderSecretStore,
    ) -> None:
        self.ssh = ssh
        self.artifacts = artifacts
        self._secrets_store = secrets_store

    async def execute(
        self,
        command: str,
        timeout_seconds: int = 20,
        max_output_bytes: int = 65_536,
    ) -> McpToolResult:
        result = await self.ssh.execute(
            command,
            timeout=timeout_seconds,
            max_output_bytes=max_output_bytes,
        )
        structured = {
            "raw_output": result.raw_output,
            "cleaned_output": result.cleaned_output,
            "status": result.status,
            "session_alive": result.session_alive,
            "safe_mode_active": result.safe_mode_active,
            "execution_time": result.execution_time,
            "output_truncated": result.output_truncated,
            "command_count": result.command_count,
            "safe_mode_action_count": result.safe_mode_action_count,
            "error_detail": result.error_detail,
        }
        content = (result.cleaned_output or result.status,)
        return McpToolResult(
            content,
            structured,
            result.status not in {"ok", "truncated"},
        )

    async def commit_and_close(self) -> bool:
        return await self.ssh.commit_and_close()

    async def abort_and_close(self) -> None:
        await self.ssh.abort_and_close()

    async def refresh_safe_mode_action_count(self) -> int | None:
        """Refresh shared Safe Mode history after a REST/MikroMCP write."""

        return await self.ssh.refresh_safe_mode_action_count()

    async def restore_full_backup(self) -> None:
        password = self._secrets_store.get(self.artifacts.backup_secret_id)
        if password is None:
            raise HighRiskError("The encrypted password for this pre-flight backup is unavailable")
        remote_name = f"mth-restore-{self.artifacts.backup_remote_name}"
        await self.ssh.restore_backup(self.artifacts.backup_path, remote_name, password)


class HighRiskService:
    """TOFU, pre-flight archives, SFTP and Safe Mode lifecycle for HIGH RISK."""

    def __init__(
        self,
        store: MikroMcpConfigStore | None = None,
        *,
        backend_factory: Callable[[], BackupBackend] | None = None,
        connection_factory: ConnectionFactory | None = None,
        secrets_store: ProviderSecretStore | None = None,
    ) -> None:
        self._store = store or MikroMcpConfigStore()
        self._backend_factory = backend_factory or self._default_backend
        self._connection_factory = connection_factory or RouterOsSshSession.open
        root = self._store.paths.root.parent
        self._backup_root = root / "high-risk-backups"
        self._secrets = secrets_store or ProviderSecretStore(
            ProviderSecretPaths(
                file=root / "high-risk-secrets.json",
                key_file=root / "high-risk-secrets.key",
            )
        )

    async def probe_host_key(self, router_id: str) -> SshHostKey:
        target = self._store.ssh_target(router_id)
        try:
            key = await asyncssh.get_server_host_key(target.host, port=target.port)
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(f"Could not read SSH host key: {error}") from error
        if key is None:
            raise HighRiskError("SSH server did not provide a host key")
        public_key = key.export_public_key("openssh").decode("ascii").strip()
        raw_fingerprint = hashlib.sha256(bytes(key.public_data)).hexdigest()
        return SshHostKey(
            router_id=router_id,
            host=target.host,
            port=target.port,
            algorithm=public_key.split(maxsplit=1)[0],
            fingerprint=key.get_fingerprint("sha256"),
            raw_fingerprint=raw_fingerprint,
            public_key=public_key,
        )

    def trust_host_key(self, host_key: SshHostKey) -> None:
        self._store.trust_ssh_host(
            host_key.router_id,
            port=host_key.port,
            fingerprint=host_key.raw_fingerprint,
            public_key=host_key.public_key,
        )

    async def enter(self, router_id: str) -> HighRiskSession:
        """Create verified artifacts, then unlock a pinned Safe Mode session."""

        target = self._store.ssh_target(router_id)
        host_key = await self.probe_host_key(router_id)
        trusted = self._store.ssh_trust(router_id)
        if trusted is None:
            raise SshTrustRequired(host_key)
        public_key, fingerprint = trusted
        if fingerprint != host_key.raw_fingerprint or public_key != host_key.public_key:
            raise HostKeyMismatchError(
                f"SSH host key mismatch for {target.host}:{target.port}; entry was blocked"
            )

        artifacts: HighRiskArtifacts | None = None
        session: RouterOsSshSession | None = None
        try:
            artifacts = self._new_artifacts(router_id)
            await self._create_remote_artifacts(artifacts)
            known_hosts = self._write_known_hosts(target, public_key)
            session = await self._connection_factory(target, known_hosts)
            artifacts.backup_path.parent.mkdir(parents=True, exist_ok=True)
            await session.download(artifacts.backup_remote_name, artifacts.backup_path)
            await session.download(artifacts.export_remote_name, artifacts.export_path)
            self._verify_artifact(artifacts.backup_path)
            self._verify_artifact(artifacts.export_path)
            delete_remote = getattr(session, "delete_remote", None)
            if callable(delete_remote):
                for remote_name in (artifacts.backup_remote_name, artifacts.export_remote_name):
                    with suppress(Exception):
                        await delete_remote(remote_name)
            self._write_manifest(artifacts, host_key)
            if not await session.enter_safe_mode():
                raise HighRiskError(
                    "RouterOS Safe Mode was not confirmed; HIGH RISK was not unlocked"
                )
            return HighRiskSession(session, artifacts, self._secrets)
        except asyncio.CancelledError:
            if session is not None:
                with suppress(Exception):
                    await asyncio.shield(session.abort_and_close())
            if artifacts is not None:
                with suppress(Exception):
                    await asyncio.shield(self._cleanup_failed_preflight(artifacts))
            raise
        except Exception:
            if session is not None:
                await session.abort_and_close()
            if artifacts is not None:
                await self._cleanup_failed_preflight(artifacts)
            raise

    def _new_artifacts(self, router_id: str) -> HighRiskArtifacts:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_router_id = "".join(
            character if character.isalnum() else "-" for character in router_id
        )
        name = f"mth-{safe_router_id}-{timestamp}-{secrets.token_hex(4)}"
        secret_id = f"backup:{router_id}:{timestamp}:{secrets.token_hex(6)}"
        password = secrets.token_urlsafe(24)
        self._secrets.set(secret_id, password)
        directory = self._backup_root / safe_router_id / f"{timestamp}-{name[-8:]}"
        return HighRiskArtifacts(
            router_id=router_id,
            created_at=timestamp,
            backup_path=directory / f"{name}.backup",
            export_path=directory / f"{name}.rsc",
            manifest_path=directory / "manifest.json",
            backup_remote_name=f"{name}.backup",
            export_remote_name=f"{name}.rsc",
            backup_secret_id=secret_id,
        )

    async def _create_remote_artifacts(self, artifacts: HighRiskArtifacts) -> None:
        password = self._secrets.get(artifacts.backup_secret_id)
        if password is None:
            raise HighRiskError("The encrypted password for this pre-flight backup is unavailable")
        backend = self._backend_factory()
        backup = await backend.call_tool(
            "create_backup",
            {
                "routerId": artifacts.router_id,
                "name": artifacts.backup_remote_name.removesuffix(".backup"),
                "password": password,
            },
        )
        if backup.is_error:
            raise HighRiskError(f"MikroMCP could not create pre-flight backup: {backup.text}")
        exported = await backend.call_tool(
            "export_config",
            {
                "routerId": artifacts.router_id,
                "file": artifacts.export_remote_name.removesuffix(".rsc"),
                "compact": False,
            },
        )
        if exported.is_error:
            raise HighRiskError(f"MikroMCP could not create pre-flight export: {exported.text}")

    async def _cleanup_failed_preflight(self, artifacts: HighRiskArtifacts) -> None:
        """Remove only this attempt's files and encrypted secret after failed entry."""

        backend: BackupBackend | None = None
        with suppress(Exception):
            backend = self._backend_factory()
        if backend is not None:
            for remote_name in (artifacts.backup_remote_name, artifacts.export_remote_name):
                with suppress(Exception):
                    arguments = {
                        "routerId": artifacts.router_id,
                        "name": remote_name,
                        "dryRun": False,
                    }
                    result = await backend.call_tool("delete_file", arguments)
                    if result.confirmation_token is not None:
                        await backend.call_tool(
                            "delete_file",
                            {**arguments, "confirmationToken": result.confirmation_token},
                        )

        for local_path in (
            artifacts.backup_path,
            artifacts.export_path,
            artifacts.manifest_path,
        ):
            with suppress(OSError):
                local_path.unlink()
        for directory in (artifacts.manifest_path.parent, artifacts.manifest_path.parent.parent):
            with suppress(OSError):
                directory.rmdir()
        with suppress(Exception):
            self._secrets.delete(artifacts.backup_secret_id)

    def _write_known_hosts(self, target: SshTarget, public_key: str) -> Path:
        host = target.host if target.port == 22 else f"[{target.host}]:{target.port}"
        path = self._store.paths.root / "ssh_known_hosts"
        self._atomic_write(path, f"{host} {public_key}\n")
        return path

    @staticmethod
    def _verify_artifact(path: Path) -> None:
        if not path.is_file() or path.stat().st_size <= 0:
            raise HighRiskError(f"Downloaded pre-flight artifact is missing or empty: {path.name}")
        try:
            with path.open("rb") as stream:
                if not stream.read(1):
                    raise HighRiskError(
                        f"Downloaded pre-flight artifact is unreadable: {path.name}"
                    )
        except OSError as error:
            raise HighRiskError(
                f"Downloaded pre-flight artifact is unreadable: {path.name}"
            ) from error

    def _write_manifest(self, artifacts: HighRiskArtifacts, host_key: SshHostKey) -> None:
        document = {
            "version": 1,
            "routerId": artifacts.router_id,
            "createdAt": artifacts.created_at,
            "sshHostKey": {"fingerprint": host_key.fingerprint, "algorithm": host_key.algorithm},
            "artifacts": {
                "backup": self._artifact_metadata(artifacts.backup_path),
                "export": self._artifact_metadata(artifacts.export_path),
            },
            "backupSecretId": artifacts.backup_secret_id,
        }
        self._atomic_write(
            artifacts.manifest_path,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        )

    @staticmethod
    def _artifact_metadata(path: Path) -> dict[str, str | int]:
        payload = path.read_bytes()
        return {
            "file": path.name,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _default_backend(self) -> BackupBackend:
        return MikroMcpClient(environment=self._store.runtime_environment())
