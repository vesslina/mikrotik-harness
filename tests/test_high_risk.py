import asyncio
from pathlib import Path

from mth.core.high_risk import HighRiskService, RouterOsSshSession
from mth.core.mcp_client.models import McpToolResult
from mth.core.registration import ConfigPaths, MikroMcpConfigStore, PendingRegistration


class _Reader:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, _size: int) -> bytes:
        return await self._queue.get()

    def put(self, value: bytes) -> None:
        self._queue.put_nowait(value)


class _Writer:
    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)
        text = payload.decode("utf-8", errors="replace")
        marker = next(
            (line.split('"')[1] for line in text.splitlines() if line.startswith(":put ")),
            None,
        )
        if marker is not None:
            prefix = "<SAFE> [admin@MikroTik] >\r\n" if b"\x18" in payload else ""
            self.reader.put((prefix + "ether1\r\n" + marker + "\r\n").encode())

    async def drain(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Process:
    def __init__(self, reader: _Reader, writer: _Writer) -> None:
        self.stdout = reader
        self.stdin = writer


def test_persistent_pty_frames_output_verifies_safe_mode_and_aborts() -> None:
    async def scenario() -> None:
        reader = _Reader()
        writer = _Writer(reader)
        connection = _Connection()
        session = RouterOsSshSession(connection, _Process(reader, writer))

        assert await session.enter_safe_mode() is True
        result = await session.execute("/interface print", max_output_bytes=1024)

        assert result.status == "ok"
        assert result.cleaned_output == "ether1"
        assert result.safe_mode_active is True
        assert result.command_count == 1
        assert all(b"__MTH_CMD_DONE_" in write for write in writer.writes[:2])

        await session.abort_and_close()
        assert connection.closed is True
        assert any(b"\x03\x04" in write for write in writer.writes)

    asyncio.run(scenario())


class _Key:
    public_data = b"test-host-key-blob"

    def export_public_key(self, _format: str) -> bytes:
        return b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestHarnessHostKey"

    def get_fingerprint(self, _hash: str) -> str:
        return "SHA256:test-host-key-fingerprint"


class _BackupBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments=None) -> McpToolResult:
        self.calls.append((name, dict(arguments or {})))
        return McpToolResult(("created",), {"filePath": "created"}, False)


class _PreflightSession:
    def __init__(self) -> None:
        self.safe_mode = False
        self.downloads: list[str] = []
        self.aborted = False

    async def download(self, remote_name: str, local_path: Path) -> None:
        self.downloads.append(remote_name)
        local_path.write_bytes(b"routeros artifact")

    async def enter_safe_mode(self) -> bool:
        self.safe_mode = True
        return True

    async def abort_and_close(self) -> None:
        self.aborted = True


def test_preflight_creates_and_verifies_local_backup_and_export(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        store = MikroMcpConfigStore(ConfigPaths(root=tmp_path / "mikromcp"))
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
        backend = _BackupBackend()
        preflight_session = _PreflightSession()

        async def connection_factory(_target, _known_hosts):
            return preflight_session

        async def get_host_key(*_args, **_kwargs):
            return _Key()

        monkeypatch.setattr(
            "mth.core.high_risk.service.asyncssh.get_server_host_key", get_host_key
        )
        service = HighRiskService(
            store,
            backend_factory=lambda: backend,
            connection_factory=connection_factory,
        )
        key = await service.probe_host_key("mikrotik-afe23e")
        service.trust_host_key(key)

        session = await service.enter("mikrotik-afe23e")

        assert [name for name, _arguments in backend.calls] == ["create_backup", "export_config"]
        assert session.artifacts.backup_path.read_bytes() == b"routeros artifact"
        assert session.artifacts.export_path.read_bytes() == b"routeros artifact"
        assert session.artifacts.manifest_path.is_file()
        assert preflight_session.safe_mode is True
        assert preflight_session.downloads == [
            session.artifacts.backup_remote_name,
            session.artifacts.export_remote_name,
        ]
        assert "router-password" not in session.artifacts.manifest_path.read_text(encoding="utf-8")

    asyncio.run(scenario())
