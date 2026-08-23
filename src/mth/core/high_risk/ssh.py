from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import asyncssh

from mth.core.high_risk.models import HighRiskError, SshExecResult
from mth.core.registration import SshTarget


class RouterOsSshSession:
    """One pinned SSH connection and one PTY which preserves RouterOS CLI state."""

    _ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    _MARKER_PREFIX = "__MTH_CMD_DONE_"

    def __init__(self, connection: Any, process: Any) -> None:
        self._connection = connection
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._lock = asyncio.Lock()
        self._pending = ""
        self._alive = True
        self._safe_mode_active = False
        self._command_count = 0

    @classmethod
    async def open(cls, target: SshTarget, known_hosts: Path) -> RouterOsSshSession:
        """Connect only through a previously written, pinned known-hosts record."""

        try:
            connection = await asyncssh.connect(
                target.host,
                port=target.port,
                username=target.username,
                password=target.password,
                known_hosts=str(known_hosts),
                keepalive_interval=20,
                keepalive_count_max=3,
            )
            process = await connection.create_process(
                request_pty=True,
                term_type="xterm",
                term_size=(160, 48, 0, 0),
                encoding=None,
            )
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(f"Could not open pinned SSH channel: {error}") from error
        return cls(connection, process)

    @property
    def safe_mode_active(self) -> bool:
        return self._safe_mode_active

    @property
    def command_count(self) -> int:
        return self._command_count

    @property
    def alive(self) -> bool:
        return self._alive

    async def enter_safe_mode(self, timeout: float = 8) -> bool:
        """Enter Safe Mode and require the RouterOS `<SAFE>` prompt as evidence."""

        async with self._lock:
            self._require_alive()
            try:
                # Ctrl+X is a terminal control key, not a RouterOS command.  It
                # must reach the PTY as a standalone byte and be allowed to
                # produce the Safe Mode prompt before we send any framing
                # command.  Sending CR/LF and :put in the same write can make
                # RouterOS treat the marker as part of the pre-Safe command,
                # leaving us with no trustworthy <SAFE> evidence.
                await self._write(b"\x18")
                raw = await self._read_until_texts(
                    ("[safe mode taken]", "<safe>"), timeout, 32_768
                )
                if "safe mode" in raw.casefold() and (
                    "another user" in raw.casefold()
                    or "hijack" in raw.casefold()
                ):
                    raise HighRiskError(
                        "RouterOS Safe Mode is owned by another session; refusing to hijack it"
                    )
                marker = self._marker()
                await self._write(self._marker_command(marker))
                await self._read_until_marker(marker, timeout, 32_768)
            except (ConnectionError, TimeoutError):
                self._alive = False
                return False
            self._safe_mode_active = "<safe>" in raw.casefold()
            return self._safe_mode_active

    async def execute(
        self,
        command: str,
        *,
        timeout: float = 20,
        max_output_bytes: int = 65_536,
    ) -> SshExecResult:
        """Execute one RouterOS line and frame its reply with a private marker."""

        self._validate_request(command, timeout, max_output_bytes)
        started = time.monotonic()
        async with self._lock:
            if not self._alive:
                return self._result("", "connection_lost", started, False)
            marker = self._marker()
            try:
                await self._write(command.encode("utf-8") + b"\r\n" + self._marker_command(marker))
                raw, truncated = await self._read_until_marker(marker, timeout, max_output_bytes)
            except TimeoutError:
                return await self._timeout_result(started, max_output_bytes)
            except ConnectionError:
                self._alive = False
                return self._result("", "connection_lost", started, False)
            self._command_count += 1
            status = "truncated" if truncated else "ok"
            return self._result(raw, status, started, truncated, command=command, marker=marker)

    async def commit_and_close(self) -> bool:
        """Release Safe Mode before closing; never use `/quit` for an unresolved session."""

        async with self._lock:
            if not self._alive:
                return False
            marker = self._marker()
            try:
                await self._write(b"\x18")
                raw = await self._read_until_text("safe mode released", 8, 32_768)
                await self._write(self._marker_command(marker))
                await self._read_until_marker(marker, 8, 32_768)
            except (ConnectionError, TimeoutError):
                return False
            released = "safe mode released" in raw.casefold() and "<safe>" not in raw.casefold()
            if released:
                self._safe_mode_active = False
                await self._close_transport()
            return released

    async def abort_and_close(self) -> None:
        """Ask RouterOS Safe Mode to roll back, then deliberately drop the transport."""

        async with self._lock:
            if self._alive and self._safe_mode_active:
                with suppress(asyncssh.Error, OSError, ConnectionError):
                    await self._write(b"\x03\x04")
            await self._close_transport()
            self._safe_mode_active = False

    async def restore_backup(self, local_backup: Path, remote_name: str, password: str) -> None:
        """Upload and load a binary backup; reboot-induced disconnect is expected success."""

        if not local_backup.is_file() or local_backup.stat().st_size <= 0:
            raise HighRiskError("The selected local binary backup is missing or empty")
        async with self._lock:
            self._require_alive()
            try:
                sftp = await self._connection.start_sftp_client()
                await sftp.put(local_backup, remote_name)
                command = f"/system backup load name={remote_name} password={password}"
                await self._write(command.encode("utf-8") + b"\r\n")
                prompt = await self._read_until_text("restore and reboot", 15, 32_768)
                if "restore and reboot" not in prompt.casefold():
                    raise HighRiskError("RouterOS did not request restore-and-reboot confirmation")
                await self._write(b"y\r\n")
            except (asyncssh.Error, OSError, ConnectionError) as error:
                raise HighRiskError(f"Could not restore binary backup: {error}") from error
            finally:
                # RouterOS reboots immediately after the affirmative answer. Do not attempt /quit.
                await self._close_transport()
                self._safe_mode_active = False

    async def download(self, remote_name: str, local_path: Path) -> None:
        """Download a remote file over an SFTP subsystem on this same SSH connection."""

        try:
            sftp = await self._connection.start_sftp_client()
            await sftp.get(remote_name, local_path)
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(
                f"Could not download RouterOS artifact {remote_name!r}: {error}"
            ) from error

    async def _timeout_result(self, started: float, max_output_bytes: int) -> SshExecResult:
        """Cancel, then re-synchronise. A failed re-sync closes a desynchronised PTY."""

        marker = self._marker()
        try:
            await self._write(b"\x03" + self._marker_command(marker))
            await self._read_until_marker(marker, 5, max_output_bytes)
        except (ConnectionError, TimeoutError):
            await self._close_transport()
            return self._result("", "desynchronized", started, False)
        return self._result("", "timeout", started, False)

    async def _read_until_text(
        self,
        needle: str,
        timeout: float,
        max_output_bytes: int,
    ) -> str:
        return await self._read_until_texts((needle,), timeout, max_output_bytes)

    async def _read_until_texts(
        self,
        needles: tuple[str, ...],
        timeout: float,
        max_output_bytes: int,
    ) -> str:
        """Read until every case-insensitive text marker is visible.

        RouterOS appends ``<SAFE>`` to the prompt rather than emitting it as a
        standalone line, so line-based framing cannot be used for the Safe
        Mode handshake.
        """

        deadline = time.monotonic() + timeout
        captured = self._pending
        self._pending = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            data = await asyncio.wait_for(self._stdout.read(4096), timeout=remaining)
            if not data:
                raise ConnectionError("SSH stream closed")
            text = self._decode(data)
            captured += text
            if len(captured.encode("utf-8")) > max_output_bytes:
                captured = captured[-max_output_bytes:]
            lowered = captured.casefold()
            if all(needle.casefold() in lowered for needle in needles):
                return captured

    async def _read_until_marker(
        self,
        marker: str,
        timeout: float,
        max_output_bytes: int,
    ) -> tuple[str, bool]:
        deadline = time.monotonic() + timeout
        captured: list[str] = []
        captured_bytes = 0
        truncated = False
        buffer = self._pending
        self._pending = ""
        while True:
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line_with_newline = line + "\n"
                if line.rstrip("\r").strip() == marker:
                    self._pending = buffer
                    return "".join(captured), truncated
                size = len(line_with_newline.encode("utf-8"))
                if captured_bytes + size <= max_output_bytes:
                    captured.append(line_with_newline)
                    captured_bytes += size
                else:
                    truncated = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._pending = buffer
                raise TimeoutError
            data = await asyncio.wait_for(self._stdout.read(4096), timeout=remaining)
            if not data:
                self._pending = buffer
                raise ConnectionError("SSH stream closed")
            buffer += self._decode(data)

    async def _write(self, payload: bytes) -> None:
        self._stdin.write(payload)
        drain = getattr(self._stdin, "drain", None)
        if callable(drain):
            await drain()

    async def _close_transport(self) -> None:
        if not self._alive:
            return
        self._alive = False
        self._connection.close()
        wait_closed = getattr(self._connection, "wait_closed", None)
        if callable(wait_closed):
            with suppress(asyncssh.Error, OSError):
                await wait_closed()

    def _result(
        self,
        raw: str,
        status: str,
        started: float,
        truncated: bool,
        *,
        command: str | None = None,
        marker: str | None = None,
    ) -> SshExecResult:
        return SshExecResult(
            raw_output=raw,
            cleaned_output=self._clean_output(raw, command, marker),
            status=status,
            session_alive=self._alive,
            safe_mode_active=self._safe_mode_active,
            execution_time=round(time.monotonic() - started, 3),
            output_truncated=truncated,
            command_count=self._command_count,
        )

    @classmethod
    def _clean_output(cls, raw: str, command: str | None, marker: str | None) -> str:
        lines: list[str] = []
        for line in cls._ANSI.sub("", raw).replace("\r", "").splitlines():
            stripped = line.strip()
            if not stripped or stripped in (command, marker):
                continue
            if marker is not None and marker in stripped and ":put" in stripped:
                continue
            lines.append(line.rstrip())
        return "\n".join(lines).strip()

    @classmethod
    def _marker(cls) -> str:
        return f"{cls._MARKER_PREFIX}{uuid.uuid4().hex}__"

    @staticmethod
    def _marker_command(marker: str) -> bytes:
        return f':put "{marker}"\r\n'.encode("ascii")

    @staticmethod
    def _decode(data: Any) -> str:
        return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)

    def _require_alive(self) -> None:
        if not self._alive:
            raise HighRiskError("The HIGH RISK SSH session is not connected")

    @staticmethod
    def _validate_request(command: str, timeout: float, max_output_bytes: int) -> None:
        if not command.strip() or "\n" in command or "\r" in command:
            raise ValueError("ssh_exec accepts exactly one non-empty RouterOS CLI line")
        if not 1 <= timeout <= 120:
            raise ValueError("ssh_exec timeout must be between 1 and 120 seconds")
        if not 256 <= max_output_bytes <= 2_000_000:
            raise ValueError("ssh_exec max_output_bytes must be between 256 and 2000000")
