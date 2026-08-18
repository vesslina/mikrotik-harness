from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class RuntimeUnavailableError(RuntimeError):
    """Raised when the pinned MikroMCP runtime has not been built."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class MikroMcpRuntime:
    backend_dir: Path = field(default_factory=lambda: project_root() / "external" / "mikromcp")
    node_command: str = "node"

    @property
    def entrypoint(self) -> Path:
        return self.backend_dir / "dist" / "main.js"

    def validate(self) -> None:
        if shutil.which(self.node_command) is None:
            raise RuntimeUnavailableError(
                "Node.js 22 or newer is required to run the MikroMCP backend."
            )
        if not self.entrypoint.is_file():
            raise RuntimeUnavailableError(
                "Pinned MikroMCP backend is not built. Run "
                "`npm --prefix external/mikromcp ci` and "
                "`npm --prefix external/mikromcp run build`."
            )

    def process_environment(self, overrides: Mapping[str, str]) -> dict[str, str]:
        env = os.environ.copy()
        env.update(overrides)
        env["MIKROMCP_TRANSPORT"] = "stdio"
        return env
