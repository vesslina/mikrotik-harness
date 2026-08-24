from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path


class RuntimeUnavailableError(RuntimeError):
    """Raised when the pinned MikroMCP runtime has not been built."""


_SNAPSHOT_RUNTIME_MARKER = '''  "tx-packet",
  "uptime",'''
_SNAPSHOT_RUNTIME_FIELDS = '''  "tx-packet",
  "uptime",
  "actual-interface",
  "slave",'''


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class MikroMcpRuntime:
    backend_dir: Path = field(default_factory=lambda: project_root() / "external" / "mikromcp")
    node_command: str = "node"

    @property
    def source_entrypoint(self) -> Path:
        return self.backend_dir / "dist" / "main.js"

    @property
    def entrypoint(self) -> Path:
        """Harness-owned compatibility build beside the ignored upstream bundle."""

        return self.backend_dir / "dist" / "mth-main.js"

    def validate(self) -> None:
        if shutil.which(self.node_command) is None:
            raise RuntimeUnavailableError(
                "Node.js 22 or newer is required to run the MikroMCP backend."
            )
        if not self.source_entrypoint.is_file():
            raise RuntimeUnavailableError(
                "Pinned MikroMCP backend is not built. Run "
                "`npm --prefix external/mikromcp ci` and "
                "`npm --prefix external/mikromcp run build`."
            )
        self._prepare_entrypoint()

    def _prepare_entrypoint(self) -> None:
        """Add two RouterOS runtime fields missing from MikroMCP's snapshot filter."""

        source = self.source_entrypoint.read_text(encoding="utf-8")
        if _SNAPSHOT_RUNTIME_FIELDS in source:
            patched = source
        elif _SNAPSHOT_RUNTIME_MARKER in source:
            patched = source.replace(
                _SNAPSHOT_RUNTIME_MARKER,
                _SNAPSHOT_RUNTIME_FIELDS,
                1,
            )
        else:
            raise RuntimeUnavailableError(
                "Pinned MikroMCP bundle has an unknown snapshot runtime-field list; "
                "refusing to apply the mth compatibility overlay."
            )

        target = self.entrypoint
        if target.is_file() and target.read_text(encoding="utf-8") == patched:
            return
        handle, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            dir=target.parent,
            text=True,
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(patched)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

    def process_environment(self, overrides: Mapping[str, str]) -> dict[str, str]:
        env = os.environ.copy()
        env.update(overrides)
        env["MIKROMCP_TRANSPORT"] = "stdio"
        return env
