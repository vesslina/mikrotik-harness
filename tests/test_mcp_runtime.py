import sys

import pytest

from mth.core.mcp_client.client import unwrap_exception_group
from mth.core.mcp_client.runtime import (
    MikroMcpRuntime,
    RuntimeUnavailableError,
    project_root,
)

UPSTREAM_BUNDLE = '''#!/usr/bin/env node
const RUNTIME_FIELDS = new Set([
  "tx-packet",
  "uptime",
  "cache-used",
  "dynamic-servers"
]);
'''


def test_project_root_can_be_set_by_installed_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MTH_PROJECT_ROOT", str(tmp_path))

    assert project_root() == tmp_path.resolve()


def test_runtime_adds_snapshot_fields_without_touching_upstream(tmp_path) -> None:
    backend = tmp_path / "mikromcp"
    source = backend / "dist" / "main.js"
    source.parent.mkdir(parents=True)
    source.write_text(UPSTREAM_BUNDLE, encoding="utf-8")
    runtime = MikroMcpRuntime(backend_dir=backend, node_command=sys.executable)

    runtime.validate()

    patched = runtime.entrypoint.read_text(encoding="utf-8")
    assert source.read_text(encoding="utf-8") == UPSTREAM_BUNDLE
    assert '"actual-interface"' in patched
    assert '"slave"' in patched
    assert '"cache-used"' in patched


def test_runtime_fails_closed_for_unknown_upstream_bundle(tmp_path) -> None:
    backend = tmp_path / "mikromcp"
    source = backend / "dist" / "main.js"
    source.parent.mkdir(parents=True)
    source.write_text("#!/usr/bin/env node\nconsole.log('changed');\n", encoding="utf-8")
    runtime = MikroMcpRuntime(backend_dir=backend, node_command=sys.executable)

    with pytest.raises(RuntimeUnavailableError, match="unknown snapshot runtime-field list"):
        runtime.validate()


def test_single_task_group_cause_is_unwrapped() -> None:
    cause = RuntimeError("HTTP_500: invalid singleton path")
    group = ExceptionGroup("task group", [ExceptionGroup("nested", [cause])])

    assert unwrap_exception_group(group) is cause
