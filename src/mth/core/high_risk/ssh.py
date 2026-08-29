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


class _TransportState:
    """Last low-level reason reported by AsyncSSH for this connection."""

    def __init__(self) -> None:
        self.lost_detail: str | None = None


class _TransportObserver(asyncssh.SSHClient):
    """Keep AsyncSSH's disconnect reason instead of reducing it to a bare EOF."""

    def __init__(self, state: _TransportState) -> None:
        self._state = state

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is None:
            self._state.lost_detail = "SSH transport was closed cleanly by the peer"
        else:
            self._state.lost_detail = f"{type(exc).__name__}: {exc}"


class RouterOsSshSession:
    """One pinned SSH connection and one PTY which preserves RouterOS CLI state."""

    _ANSI = re.compile(r"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~])")
    _PROMPT = re.compile(r"^\[[^\]\r\n]+\]\s+(?:<SAFE>\s+)?")
    _MARKER_PREFIX = "__MTH_CMD_DONE_"

    def __init__(
        self,
        connection: Any,
        process: Any,
        transport_state: _TransportState | None = None,
    ) -> None:
        self._connection = connection
        self._process = process
        self._stdin = process.stdin
        self._stdout = process.stdout
        self._lock = asyncio.Lock()
        self._pending = ""
        self._alive = True
        self._safe_mode_active = False
        self._command_count = 0
        self._safe_mode_action_count: int | None = None
        self._term_rows = 48
        self._term_cols = 160
        self._cursor_row = 1
        self._cursor_col = 1
        self._saved_cursor = (1, 1)
        self._terminal_parse_buffer = b""
        self._transport_state = transport_state or _TransportState()
        self._last_transport_error: str | None = None

    @classmethod
    async def open(cls, target: SshTarget, known_hosts: Path) -> RouterOsSshSession:
        """Connect only through a previously written, pinned known-hosts record."""

        transport_state = _TransportState()
        try:
            connection = await asyncssh.connect(
                target.host,
                port=target.port,
                username=target.username,
                password=target.password,
                known_hosts=str(known_hosts),
                # RouterOS 7.21 does not acknowledge AsyncSSH's application-level
                # keepalive global requests consistently. AsyncSSH interprets the
                # missing replies as a dead peer and closes a healthy transport
                # after roughly 80 seconds. TCP plus command framing provide the
                # liveness boundary here; a future heartbeat must be a real,
                # harmless RouterOS CLI marker on this persistent PTY.
                keepalive_interval=0,
                client_factory=lambda: _TransportObserver(transport_state),
            )
            process = await connection.create_process(
                request_pty=True,
                term_type="xterm",
                term_size=(160, 48, 0, 0),
                encoding=None,
            )
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(f"Could not open pinned SSH channel: {error}") from error
        session = cls(connection, process, transport_state)
        try:
            await session._synchronise_terminal()
        except Exception:
            await session._close_transport()
            raise
        return session

    @property
    def safe_mode_active(self) -> bool:
        return self._safe_mode_active

    @property
    def command_count(self) -> int:
        return self._command_count

    @property
    def safe_mode_action_count(self) -> int | None:
        """Best-effort count of RouterOS floating-undo actions in Safe Mode."""

        return self._safe_mode_action_count

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
                raw = await self._read_until_safe_mode(timeout, 32_768)
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
            except (asyncssh.Error, OSError, ConnectionError, TimeoutError) as error:
                self._last_transport_error = self._transport_diagnostic(error)
                await self._close_transport()
                return False
            self._safe_mode_active = "<safe>" in raw.casefold()
            if self._safe_mode_active:
                self._safe_mode_action_count = 0
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
                self._safe_mode_active = False
                return self._result(
                    "",
                    "connection_lost",
                    started,
                    False,
                    error_detail=self._last_transport_error or "SSH session is already closed",
                )
            marker = self._marker()
            try:
                await self._write(command.encode("utf-8") + b"\r\n" + self._marker_command(marker))
                raw, truncated = await self._read_until_marker(marker, timeout, max_output_bytes)
            except TimeoutError:
                return await self._timeout_result(started, max_output_bytes)
            except (asyncssh.Error, OSError, ConnectionError) as error:
                detail = self._transport_diagnostic(error)
                self._mark_transport_lost(detail)
                return self._result(
                    "", "connection_lost", started, False, error_detail=detail
                )
            self._command_count += 1
            if self._safe_mode_active:
                await self._refresh_safe_mode_action_count()
            status = "truncated" if truncated else "ok"
            return self._result(raw, status, started, truncated, command=command, marker=marker)

    async def commit_and_close(self) -> bool:
        """Release Safe Mode before closing; never use `/quit` for an unresolved session."""

        async with self._lock:
            if not self._alive:
                return False
            marker = self._marker()
            try:
                # Drop the prompt left by the last framed command.  Release
                # verification must inspect the prompt produced by this Ctrl+X.
                self._pending = ""
                await self._write(b"\x18")
                raw = await self._read_until_prompt_state(8, 32_768, safe=False)
                await self._write(self._marker_command(marker))
                marker_output, _ = await self._read_until_marker(marker, 8, 32_768)
            except (asyncssh.Error, OSError, ConnectionError, TimeoutError) as error:
                self._mark_transport_lost(self._transport_diagnostic(error))
                return False
            released = self._prompt_is_safe(raw + marker_output) is False
            if released:
                self._safe_mode_active = False
                self._safe_mode_action_count = None
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
            self._safe_mode_action_count = None

    async def restore_backup(self, local_backup: Path, remote_name: str, password: str) -> None:
        """Upload and load a binary backup; reboot-induced disconnect is expected success."""

        if not local_backup.is_file() or local_backup.stat().st_size <= 0:
            raise HighRiskError("The selected local binary backup is missing or empty")
        async with self._lock:
            self._require_alive()
            try:
                sftp = await self._connection.start_sftp_client()
                async with sftp:
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
            async with sftp:
                await sftp.get(remote_name, local_path)
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(
                f"Could not download RouterOS artifact {remote_name!r}: {error}"
            ) from error

    async def delete_remote(self, remote_name: str) -> None:
        """Delete a pre-flight artifact through the already pinned SSH channel."""

        try:
            sftp = await self._connection.start_sftp_client()
            async with sftp:
                await sftp.remove(remote_name)
        except (asyncssh.Error, OSError) as error:
            raise HighRiskError(
                f"Could not delete RouterOS artifact {remote_name!r}: {error}"
            ) from error

    async def _timeout_result(self, started: float, max_output_bytes: int) -> SshExecResult:
        """Cancel, then re-synchronise. A failed re-sync closes a desynchronised PTY."""

        marker = self._marker()
        try:
            await self._write(b"\x03" + self._marker_command(marker))
            await self._read_until_marker(marker, 5, max_output_bytes)
        except (asyncssh.Error, OSError, ConnectionError, TimeoutError) as error:
            self._last_transport_error = self._transport_diagnostic(error)
            await self._close_transport()
            return self._result(
                "",
                "desynchronized",
                started,
                False,
                error_detail=self._last_transport_error,
            )
        return self._result("", "timeout", started, False)

    async def _refresh_safe_mode_action_count(self) -> None:
        """Read RouterOS' floating-undo history without adding a model command."""

        marker = self._marker()
        try:
            await self._write(
                b"/system history print detail\r\n" + self._marker_command(marker)
            )
            raw, _ = await self._read_until_marker(marker, 5, 32_768)
        except (asyncssh.Error, OSError, ConnectionError, TimeoutError) as error:
            # A missing frame leaves the PTY state untrusted just like any
            # other transport failure; close it rather than continuing under
            # an unverified Safe Mode state.
            self._mark_transport_lost(self._transport_diagnostic(error))
            await self._close_transport()
            self._safe_mode_action_count = None
            return
        self._safe_mode_action_count = self._count_floating_undo(raw)

    async def refresh_safe_mode_action_count(self) -> int | None:
        """Refresh the Safe Mode counter after a change made outside this PTY.

        MikroMCP writes use its REST connection, but RouterOS Safe Mode history
        is shared across sessions.  Keep the displayed counter honest without
        making the model spend a tool round on an internal history read.
        """

        async with self._lock:
            if not self._alive or not self._safe_mode_active:
                return None
            await self._refresh_safe_mode_action_count()
            return self._safe_mode_action_count

    @classmethod
    def _count_floating_undo(cls, raw: str) -> int:
        normalized = cls._ANSI.sub("", raw.replace("\x9b", "\x1b["))
        return sum(
            1 for line in normalized.replace("\r", "").splitlines()
            if re.match(r"^\s*F(?:\s|$)", line)
        )

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
            data = await self._read_stdout(remaining)
            if not data:
                raise ConnectionError("SSH stream closed")
            text = self._decode(data)
            captured += text
            if len(captured.encode("utf-8")) > max_output_bytes:
                captured = captured[-max_output_bytes:]
            lowered = captured.casefold()
            if all(needle.casefold() in lowered for needle in needles):
                return captured

    async def _read_until_prompt_state(
        self,
        timeout: float,
        max_output_bytes: int,
        *,
        safe: bool,
    ) -> str:
        """Wait for a RouterOS prompt and verify whether it carries ``<SAFE>``."""

        deadline = time.monotonic() + timeout
        captured = self._pending
        self._pending = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            data = await self._read_stdout(remaining)
            if not data:
                raise ConnectionError("SSH stream closed")
            captured += self._decode(data)
            if len(captured.encode("utf-8")) > max_output_bytes:
                captured = captured[-max_output_bytes:]
            if self._prompt_is_safe(captured) is safe:
                return captured

    @classmethod
    def _prompt_is_safe(cls, text: str) -> bool | None:
        normalized = cls._ANSI.sub("", text.replace("\x9b", "\x1b["))
        prompts = re.findall(r"\[[^\]\r\n]+\]\s+(?:<SAFE>\s+)?", normalized, re.IGNORECASE)
        if not prompts:
            return None
        return "<safe>" in prompts[-1].casefold()

    async def _read_until_safe_mode(self, timeout: float, max_output_bytes: int) -> str:
        """Wait for RouterOS' version-specific Safe Mode confirmation.

        RouterOS 7.21 emits ``Taking Safe Mode session...`` followed by
        ``Success!`` and a prompt containing ``<SAFE>``. Older releases use the
        ``[Safe Mode taken]`` line. Both are valid evidence; a bare prompt is
        intentionally not enough.
        """

        deadline = time.monotonic() + timeout
        captured = self._pending
        self._pending = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            data = await self._read_stdout(remaining)
            if not data:
                raise ConnectionError("SSH stream closed")
            captured += self._decode(data)
            if len(captured.encode("utf-8")) > max_output_bytes:
                captured = captured[-max_output_bytes:]
            lowered = captured.casefold()
            if "safe mode" in lowered and (
                "another user" in lowered or "hijack" in lowered
            ):
                raise HighRiskError(
                    "RouterOS Safe Mode is owned by another session; refusing to hijack it"
                )
            if "<safe>" in lowered and (
                "[safe mode taken]" in lowered or "success!" in lowered
            ):
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
            data = await self._read_stdout(remaining)
            if not data:
                self._pending = buffer
                raise ConnectionError("SSH stream closed")
            buffer += self._decode(data)

    async def _synchronise_terminal(self, timeout: float = 8) -> None:
        """Complete RouterOS' terminal capability handshake before CLI input.

        RouterOS asks for terminal identification and cursor position while a
        PTY is starting. AsyncSSH transports the PTY bytes but intentionally does
        not emulate a terminal, so without these replies the RouterOS prompt is
        never opened and control keys (including Safe Mode) are ignored.
        """

        deadline = time.monotonic() + timeout
        captured = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HighRiskError(
                    "RouterOS SSH terminal negotiation timed out before the CLI prompt"
                )
            data = await self._read_stdout(remaining)
            if not data:
                raise ConnectionError("SSH stream closed during terminal negotiation")
            captured += self._terminal_text(data)
            if len(captured) > 32_768:
                captured = captured[-32_768:]
            # RouterOS emits colour using either ESC[... or the 8-bit CSI form;
            # _terminal_text normalises both before this prompt check.
            if "] >" in captured and ("admin@" in captured or "MikroTik" in captured):
                return

    async def _read_stdout(self, timeout: float) -> bytes:
        data = await asyncio.wait_for(self._stdout.read(4096), timeout=timeout)
        if not data:
            return b""
        payload = data if isinstance(data, bytes) else str(data).encode("utf-8")
        await self._respond_to_terminal_queries(payload)
        return payload

    async def _respond_to_terminal_queries(self, data: bytes) -> None:
        """Answer the small VT query subset RouterOS uses during PTY startup."""

        buffer = self._terminal_parse_buffer + data
        self._terminal_parse_buffer = b""
        responses: list[bytes] = []
        index = 0
        while index < len(buffer):
            byte = buffer[index]
            if byte == 0x1B:
                if index + 1 >= len(buffer):
                    self._terminal_parse_buffer = buffer[index:]
                    break
                if buffer[index + 1] == ord("["):
                    end = self._find_csi_end(buffer, index + 2)
                    if end is None:
                        self._terminal_parse_buffer = buffer[index:]
                        break
                    body = buffer[index + 2 : end]
                    final = chr(buffer[end])
                    response = self._handle_csi(body, final)
                    if response is not None:
                        responses.append(response)
                    index = end + 1
                    continue
                if buffer[index + 1] == ord("Z"):
                    # DECID: identify as a basic ANSI/VT terminal.
                    responses.append(b"\x1b[?1;2c")
                    index += 2
                    continue
                self._handle_escape(buffer[index + 1])
                index += 2
                continue
            if byte == 0x9B:
                end = self._find_csi_end(buffer, index + 1)
                if end is None:
                    self._terminal_parse_buffer = buffer[index:]
                    break
                body = buffer[index + 1 : end]
                final = chr(buffer[end])
                response = self._handle_csi(body, final)
                if response is not None:
                    responses.append(response)
                index = end + 1
                continue
            self._advance_cursor_byte(byte)
            index += 1
        if responses:
            await self._write(b"".join(responses))

    @staticmethod
    def _find_csi_end(data: bytes, start: int) -> int | None:
        for index in range(start, len(data)):
            if 0x40 <= data[index] <= 0x7E:
                return index
        return None

    def _handle_csi(self, body: bytes, final: str) -> bytes | None:
        text = body.decode("ascii", errors="ignore")
        private = text.startswith("?")
        if private:
            text = text[1:]
        params = self._csi_params(text)
        if final == "n" and text == "6":
            return f"\x1b[{self._cursor_row};{self._cursor_col}R".encode("ascii")
        if private:
            return None
        if final in {"H", "f"}:
            self._cursor_row = self._bounded(params[0], 1, self._term_rows)
            self._cursor_col = self._bounded(
                params[1] if len(params) > 1 else 1, 1, self._term_cols
            )
        elif final == "A":
            self._cursor_row = max(1, self._cursor_row - (params[0] or 1))
        elif final == "B":
            self._cursor_row = min(self._term_rows, self._cursor_row + (params[0] or 1))
        elif final == "C":
            self._cursor_col = min(self._term_cols, self._cursor_col + (params[0] or 1))
        elif final == "D":
            self._cursor_col = max(1, self._cursor_col - (params[0] or 1))
        elif final in {"G", "`"}:
            self._cursor_col = self._bounded(params[0], 1, self._term_cols)
        elif final == "d":
            self._cursor_row = self._bounded(params[0], 1, self._term_rows)
        elif final == "s":
            self._saved_cursor = (self._cursor_row, self._cursor_col)
        elif final == "u":
            self._cursor_row, self._cursor_col = self._saved_cursor
        elif final == "E":
            self._cursor_row = min(self._term_rows, self._cursor_row + (params[0] or 1))
            self._cursor_col = 1
        elif final == "F":
            self._cursor_row = max(1, self._cursor_row - (params[0] or 1))
            self._cursor_col = 1
        return None

    @staticmethod
    def _csi_params(text: str) -> list[int]:
        if not text:
            return [1]
        values: list[int] = []
        for item in text.split(";"):
            try:
                values.append(int(item) if item else 1)
            except ValueError:
                values.append(1)
        return values

    @staticmethod
    def _bounded(value: int, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, value or minimum))

    def _handle_escape(self, final: int) -> None:
        if final == ord("D"):
            self._cursor_row = min(self._term_rows, self._cursor_row + 1)
        elif final == ord("M"):
            self._cursor_row = max(1, self._cursor_row - 1)
        elif final == ord("E"):
            self._cursor_row = min(self._term_rows, self._cursor_row + 1)
            self._cursor_col = 1
        elif final == ord("7"):
            self._saved_cursor = (self._cursor_row, self._cursor_col)
        elif final == ord("8"):
            self._cursor_row, self._cursor_col = self._saved_cursor
        elif final == ord("c"):
            self._cursor_row, self._cursor_col = 1, 1

    def _advance_cursor_byte(self, byte: int) -> None:
        if byte == 0x0D:
            self._cursor_col = 1
        elif byte == 0x0A:
            self._cursor_row = min(self._term_rows, self._cursor_row + 1)
        elif byte == 0x08:
            self._cursor_col = max(1, self._cursor_col - 1)
        elif byte == 0x09:
            self._cursor_col = min(self._term_cols, ((self._cursor_col - 1) // 8 + 1) * 8 + 1)
        elif byte >= 0x20:
            self._cursor_col = min(self._term_cols, self._cursor_col + 1)

    @staticmethod
    def _terminal_text(data: bytes) -> str:
        return data.replace(b"\x9b", b"\x1b[").decode("utf-8", errors="replace")

    async def _write(self, payload: bytes) -> None:
        self._stdin.write(payload)
        drain = getattr(self._stdin, "drain", None)
        if callable(drain):
            await drain()

    async def _close_transport(self) -> None:
        if not self._alive:
            is_closed = getattr(self._connection, "is_closed", None)
            if callable(is_closed) and is_closed():
                return
        self._alive = False
        self._connection.close()
        wait_closed = getattr(self._connection, "wait_closed", None)
        if callable(wait_closed):
            with suppress(asyncssh.Error, OSError):
                await wait_closed()

    def _mark_transport_lost(self, detail: str) -> None:
        """Invalidate the session when the channel disappears unexpectedly.

        Once the SSH transport is gone we cannot prove whether Safe Mode was
        released or rolled back.  Reporting it as active would invite the
        agent/operator to continue on a session that no longer exists, so the
        state is deliberately fail-closed.
        """

        self._alive = False
        self._safe_mode_active = False
        self._last_transport_error = detail

    def _transport_diagnostic(self, error: BaseException) -> str:
        """Describe whether the whole connection or only the RouterOS PTY ended."""

        details: list[str] = []
        if self._transport_state.lost_detail:
            details.append(self._transport_state.lost_detail)
        fallback = f"{type(error).__name__}: {error}"
        if fallback not in details:
            details.append(fallback)

        is_closed = getattr(self._connection, "is_closed", None)
        if callable(is_closed):
            details.append(f"connection_closed={bool(is_closed())}")
        is_closing = getattr(self._process, "is_closing", None)
        if callable(is_closing):
            details.append(f"pty_closing={bool(is_closing())}")
        returncode = getattr(self._process, "returncode", None)
        if returncode is not None:
            details.append(f"pty_returncode={returncode}")
        exit_signal = getattr(self._process, "exit_signal", None)
        if exit_signal:
            details.append(f"pty_exit_signal={exit_signal}")
        return "; ".join(details)

    def _result(
        self,
        raw: str,
        status: str,
        started: float,
        truncated: bool,
        *,
        command: str | None = None,
        marker: str | None = None,
        error_detail: str | None = None,
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
            safe_mode_action_count=self._safe_mode_action_count,
            error_detail=error_detail,
        )

    @classmethod
    def _clean_output(cls, raw: str, command: str | None, marker: str | None) -> str:
        lines: list[str] = []
        normalized = raw.replace("\x9b", "\x1b[")
        for line in cls._ANSI.sub("", normalized).replace("\r", "").splitlines():
            line = cls._PROMPT.sub("", line)
            stripped = line.strip()
            if not stripped or stripped in (command, marker):
                continue
            if (
                command
                and stripped.startswith(command)
                and stripped[len(command) :].strip() == command
            ):
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
        if isinstance(data, bytes):
            return data.replace(b"\x9b", b"\x1b[").decode("utf-8", errors="replace")
        return str(data)

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
