import asyncio
from pathlib import Path
from types import SimpleNamespace

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


class _ClosedReader:
    async def read(self, _size: int) -> bytes:
        return b""


class _ClosedWriter:
    def write(self, _payload: bytes) -> None:
        return None

    async def drain(self) -> None:
        return None


class _Writer:
    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)
        text = payload.decode("utf-8", errors="replace")
        if b"\x18" in payload and b":put " not in payload:
            self.reader.put(
                b"Taking Safe Mode session... Success!\r\n"
                b"[admin@MikroTik] <SAFE> >\r\n"
            )
            return
        marker = next(
            (line.split('"')[1] for line in text.splitlines() if line.startswith(":put ")),
            None,
        )
        if marker is not None:
            self.reader.put(("ether1\r\n" + marker + "\r\n").encode())

    async def drain(self) -> None:
        return None


class _ReleaseWriter(_Writer):
    def __init__(self, reader: _Reader) -> None:
        super().__init__(reader)
        self.safe = False

    def write(self, payload: bytes) -> None:
        if b"\x18" in payload and b":put " not in payload:
            self.writes.append(payload)
            if self.safe:
                self.safe = False
                self.reader.put(b"[session committed]\r\n[admin@MikroTik] >\r\n")
            else:
                self.safe = True
                self.reader.put(
                    b"Taking Safe Mode session... Success!\r\n"
                    b"[admin@MikroTik] <SAFE> >\r\n"
                )
            return
        super().write(payload)


class _Connection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _SftpClient:
    def __init__(self) -> None:
        self.closed = False
        self.gets: list[tuple[str, Path]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> bool:
        self.closed = True
        return False

    async def get(self, remote_name: str, local_path: Path) -> None:
        self.gets.append((remote_name, local_path))
        local_path.write_bytes(b"artifact")


class _SftpConnection(_Connection):
    def __init__(self) -> None:
        super().__init__()
        self.clients: list[_SftpClient] = []

    async def start_sftp_client(self) -> _SftpClient:
        client = _SftpClient()
        self.clients.append(client)
        return client


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
        assert b"\x18" in writer.writes[0]
        assert b"__MTH_CMD_DONE_" in writer.writes[1]

        await session.abort_and_close()
        assert connection.closed is True
        assert any(b"\x03\x04" in write for write in writer.writes)

    asyncio.run(scenario())


def test_commit_verifies_release_from_the_new_non_safe_prompt() -> None:
    async def scenario() -> None:
        reader = _Reader()
        writer = _ReleaseWriter(reader)
        connection = _Connection()
        session = RouterOsSshSession(connection, _Process(reader, writer))

        assert await session.enter_safe_mode() is True
        assert await session.commit_and_close() is True
        assert session.safe_mode_active is False
        assert connection.closed is True

    asyncio.run(scenario())


def test_routeros_terminal_negotiation_answers_cursor_queries() -> None:
    async def scenario() -> None:
        reader = _Reader()
        writer = _Writer(reader)
        connection = _Connection()
        session = RouterOsSshSession(connection, _Process(reader, writer))
        reader.put(
            b"\r\x1b[9999B\r\x1b[9999B\x1bZ  \x1b[6n"
            b"\x1b[H\x1b[9999B\x1b[6n"
            b"\x1b[H\x1b[9999C\x1b[6n"
            b"\r\n[admin@MikroTik] > "
        )

        await session._synchronise_terminal(timeout=1)

        assert any(b"\x1b[?1;2c" in payload for payload in writer.writes)
        assert sum(payload.count(b"R") for payload in writer.writes) >= 3

    asyncio.run(scenario())


def test_open_disables_incompatible_asyncssh_keepalive(monkeypatch, tmp_path) -> None:
    async def scenario() -> None:
        reader = _Reader()
        writer = _Writer(reader)
        connection = _Connection()
        captured: dict[str, object] = {}

        async def connect(_host, **kwargs):
            captured.update(kwargs)

            async def create_process(**_process_kwargs):
                reader.put(b"[admin@MikroTik] > ")
                return _Process(reader, writer)

            connection.create_process = create_process  # type: ignore[attr-defined]
            return connection

        monkeypatch.setattr("mth.core.high_risk.ssh.asyncssh.connect", connect)
        target = SimpleNamespace(
            host="192.0.2.1",
            port=22,
            username="admin",
            password="secret",
        )

        session = await RouterOsSshSession.open(target, tmp_path / "known_hosts")  # type: ignore[arg-type]

        assert captured["keepalive_interval"] == 0
        assert "keepalive_count_max" not in captured
        await session.abort_and_close()

    asyncio.run(scenario())


def test_connection_loss_invalidates_safe_mode_state() -> None:
    async def scenario() -> None:
        reader = _ClosedReader()
        writer = _ClosedWriter()
        connection = _Connection()
        session = RouterOsSshSession(connection, _Process(reader, writer))  # type: ignore[arg-type]
        session._safe_mode_active = True

        result = await session.execute("/system identity print")

        assert result.status == "connection_lost"
        assert result.session_alive is False
        assert result.safe_mode_active is False
        assert session.alive is False
        assert result.error_detail == "ConnectionError: SSH stream closed"

    asyncio.run(scenario())


def test_sftp_download_closes_subsystem_channel(tmp_path) -> None:
    async def scenario() -> None:
        reader = _Reader()
        writer = _Writer(reader)
        connection = _SftpConnection()
        session = RouterOsSshSession(connection, _Process(reader, writer))
        local_path = tmp_path / "router.backup"

        await session.download("router.backup", local_path)

        assert local_path.read_bytes() == b"artifact"
        assert len(connection.clients) == 1
        assert connection.clients[0].closed is True

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


class _FailingPreflightSession(_PreflightSession):
    async def download(self, remote_name: str, local_path: Path) -> None:
        await super().download(remote_name, local_path)
        if len(self.downloads) == 2:
            raise OSError("simulated partial SFTP download")


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


def test_failed_preflight_cleans_remote_files_local_partials_and_secret(
    monkeypatch, tmp_path
) -> None:
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
        preflight_session = _FailingPreflightSession()

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

        try:
            await service.enter("mikrotik-afe23e")
        except OSError:
            pass
        else:
            raise AssertionError("preflight should fail")

        assert preflight_session.aborted is True
        assert [name for name, _arguments in backend.calls] == [
            "create_backup",
            "export_config",
            "delete_file",
            "delete_file",
        ]
        delete_arguments = [arguments for name, arguments in backend.calls if name == "delete_file"]
        assert {arguments["name"] for arguments in delete_arguments} == {
            backend.calls[0][1]["name"] + ".backup",
            backend.calls[0][1]["name"] + ".rsc",
        }
        assert not list((tmp_path / "high-risk-backups").rglob("*"))
        assert service._secrets._load()["secrets"] == {}

    asyncio.run(scenario())


def test_cancelled_preflight_also_cleans_secret_and_remote_artifacts(monkeypatch, tmp_path) -> None:
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

        async def connection_factory(_target, _known_hosts):
            raise asyncio.CancelledError

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

        try:
            await service.enter("mikrotik-afe23e")
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("preflight cancellation should propagate")

        assert [name for name, _arguments in backend.calls] == [
            "create_backup",
            "export_config",
            "delete_file",
            "delete_file",
        ]
        assert service._secrets._load()["secrets"] == {}

    asyncio.run(scenario())
