# Windows offline distribution

Status: distribution contract for the current beta release candidate. The bundle is not a supported release
artifact until every clean-machine check below passes.

## Decision

The field edition will be a self-contained, per-user ZIP bundle installed by one PowerShell script.
It will not copy a development virtual environment and will not compile anything on the target
computer.

Two release candidates are built from the same commit:

- `MikroTikHarness-<version>-win-x64-py312.zip` — primary field build;
- `MikroTikHarness-<version>-win-x64-py311.zip` — compatibility build and test artifact.

Each bundle contains one Python version. Shipping both interpreters in one installer adds size and
branches without improving the operator workflow.

## Target machine

Required:

- Windows 10 or Windows 11, x64;
- enough local disk for the installed bundle and recovery artifacts;
- network access to the RouterOS management services;
- an LLM endpoint when the operator wants to use the agent.

Not required:

- administrator rights;
- internet access;
- Git, npm, PowerShell 7, Visual Studio, or C++ Build Tools;
- globally installed Python or Node.js;
- a separately configured system `PATH` for Python or Node.js.

Windows PowerShell 5.1 runs the installer. The application uses its own pinned Python and Node.js.
The packager must use binary Python wheels only; a missing wheel is a build failure, not a reason to
install a compiler on a field laptop.

A separate VC++ Redistributable is not currently part of the bundle. The full CPython installer
supplies its Python runtime files and the selected wheels must be import-tested on a clean Windows
image. If that test proves an additional Microsoft runtime is required, the official redistributable
is added as a verified bundle input; it must not be guessed or silently downloaded by the installer.

## Bundle layout

```text
MikroTikHarness-<version>-win-x64-py312/
├── install.ps1
├── uninstall.ps1
├── manifest.sha256
├── LICENSE
├── THIRD_PARTY_NOTICES
├── runtime/
│   ├── python-<exact-version>-amd64.exe
│   └── node-v<exact-version>-win-x64.zip
├── wheelhouse/
│   ├── mikrotik_harness-<version>-py3-none-any.whl
│   ├── pip-<safe-version>-py3-none-any.whl
│   ├── setuptools-<safe-version>-py3-none-any.whl
│   └── <resolved binary dependency wheels>
├── app/
│   ├── external/mikromcp/
│   │   ├── dist/main.js
│   │   ├── node_modules/<production dependencies>
│   │   ├── package.json
│   │   └── LICENSE
│   └── docs/field-recipes/*.md
└── optional-private/
    └── .mth/rag/<operator-built RouterOS manual pack>
```

`optional-private/.mth/rag` is used for an internal field package, not the public GitHub release.
The public project does not redistribute the MikroTik documentation corpus.

Every payload file in the ZIP (the manifest itself is the checksum index) is covered by
`manifest.sha256`. The release builder records the exact
Python installer, Node ZIP, Python wheels, MikroMCP git commit, npm lockfile, and licenses used.

## Installer contract

`install.ps1` is deliberately small and idempotent:

1. Require native x64 Windows 10 or newer.
2. Verify `manifest.sha256` before executing bundled binaries.
3. Reuse an existing compatible 64-bit CPython of the bundle's minor version when one is
   available. Otherwise install the bundled CPython per user into
   `%LOCALAPPDATA%\Programs\MikroTikHarness\runtime\python` with no launcher and no global PATH
   change. Verify the interpreter before continuing.
4. Extract the private Node.js ZIP under the same application directory and verify Node.js 22+.
5. Create a fresh venv, upgrade its bundled pip/setuptools from the local bootstrap wheels, and
   install with `--no-index --only-binary=:all: --find-links <wheelhouse>`.
6. Copy the prebuilt MikroMCP runtime, project-owned field cards, and an optional private RAG pack.
7. Create `bin\mth.cmd`. The launcher sets `MTH_PROJECT_ROOT`, prepends the private Node
   directory only for this process, and calls the installed `mth.exe`.
8. Add only the application `bin` directory to the current user's PATH when the operator passes
   the explicit `-AddToPath` switch.
9. Run the offline import, CLI-help, and live bundled MikroMCP catalog smoke checks below and fail
   without deleting diagnostic logs.

The application always runs from its own venv; it never installs Python packages into an existing
interpreter. When an existing CPython is reused, that base interpreter must remain installed for
the venv to work. Node.js is always extracted from the bundled ZIP into the application directory,
so an existing system Node.js installation cannot conflict with it.

The installer never invokes Git, npm, PyPI, GitHub, or the MikroTik documentation site. Re-running
the same version repairs missing application files without deleting `.mth` user data.
`uninstall.ps1` removes the application and PATH entry but requires a separate explicit choice
before deleting private state or HIGH RISK backups.

## Build machine contract

The release builder is the only machine that needs internet, Git, and npm.

1. Check out the release commit with `--recurse-submodules`.
2. Verify that `external/mikromcp` is the gitlink commit recorded by the harness repository.
3. Install the build environment from `.[dev]` plus `pip-audit`; the builder reruns the full
   Python gate before creating any output.
4. Run `npm.cmd ci`, the MikroMCP checks, and `npm.cmd run build`.
5. Copy `dist/main.js` and production npm dependencies; do not copy the development checkout.
6. Build the harness wheel.
7. Resolve a locked Python dependency set separately for CPython 3.11 and 3.12 on win_amd64.
8. Download only wheels. `pip download --only-binary=:all:` must fail if any dependency would
   require a source build.
9. Generate `THIRD_PARTY_NOTICES` and `manifest.sha256`.
10. Build the two ZIPs and test each from a clean Windows snapshot with networking disabled.

## Dependency inventory

Direct Python runtime requirements are defined in `pyproject.toml`:

- AsyncSSH;
- cryptography;
- the official Python MCP SDK;
- PyYAML;
- Textual.

Their transitive dependencies are release inputs, not a hand-maintained list. Each wheel filename,
the lockfile, and the generated `manifest.sha256` record the exact files shipped in that release.
Developer-only pytest, Ruff, mypy, and typing stubs are excluded from the field bundle.

MikroMCP v1.10.0 requires Node.js 22 or newer. Its JavaScript dependencies are resolved only from
the pinned upstream `package-lock.json`. The release gate runs `npm audit --omit=dev` because the
bundle contains only production `node_modules`; development-only advisories printed by `npm ci` do
not ship to an operator machine. The target does not run npm.

The RouterOS manual pack and HIGH RISK backups are application data, not code dependencies. A full
`.mth/rag` directory can be copied from a USB drive. If it is absent, the rest of `mth` still
works; documentation search reports that no local pack is installed.

## Offline smoke checks

The installer must run these checks without internet:

1. Private `python.exe --version` matches the bundle.
2. Private `node.exe --version` satisfies MikroMCP.
3. Import `asyncssh`, `cryptography`, `mcp`, `yaml`, `textual`, and `mth`.
4. `mth --help` and `mth discover --help` exit successfully.
5. MikroMCP entrypoint exists and starts over stdio long enough for an MCP initialize/tools-list
   smoke test.
6. When a RAG pack is included, `mth rag --query "ip address"` returns a local result while all
   network adapters are disabled.
7. `mth` can be launched from a newly opened Windows PowerShell by typing only `mth`.

## Clean-machine acceptance matrix

Run every row from a restored VM snapshot with no Python, Node.js, Git, npm, Visual Studio, or
project files installed beforehand.

| Case | OS | Bundle | Network during install | Expected |
| --- | --- | --- | --- | --- |
| A | Windows 10 x64 | CPython 3.11 | Disabled | Install and offline smoke checks pass |
| B | Windows 10 x64 | CPython 3.12 | Disabled | Install and offline smoke checks pass |
| C | Windows 11 x64 | CPython 3.12 | Disabled | Install and offline smoke checks pass |
| D | Windows 10 x64 | CPython 3.12 + copied RAG pack | Disabled | Local RAG survives restart and returns sources |
| E | Windows 10 x64 | Reinstall same version | Disabled | App is repaired; `.mth` state remains |
| F | Windows 10 x64 | Uninstall | Disabled | Runtime/PATH removed; private data retained by default |

After the installation matrix, perform one live-router matrix: Discovery and TLS registration,
LM Studio, Ollama-compatible mock or real endpoint, arbitrary OpenAI-compatible provider, PLAN,
READY approval/apply/verify, HIGH RISK pre-flight/CLI/commit, Safe Mode abort, and full backup
restore.

## Why not copy the project folder

A copied Windows `.venv` contains absolute interpreter paths and is not a portable application.
A source folder also assumes compatible global Python, Node.js, npm state, and a populated
submodule. Building an EXE with another packaging layer would hide these assumptions rather than
remove them. The private-runtime ZIP keeps the original runtimes visible, pinned, replaceable, and
easy to diagnose.

## Build commands

Run these commands on a connected Windows build machine from a clean checkout. The two runtime
archives are explicit inputs, so a build cannot accidentally produce a bundle that later needs the
internet:

```powershell
pwsh -NoProfile -File .\scripts\build-offline.ps1 `
  -PythonVersion 3.12 `
  -PythonInstaller .\inputs\python-3.12.x-amd64.exe `
  -NodeArchive .\inputs\node-v22.x-win-x64.zip
```

Use `-PythonVersion 3.11` for the compatibility bundle. Add `-RagPack <directory>` only for a
private field bundle; the public artifact must omit the copyrighted manual corpus. The builder
fails on a dirty checkout unless `-AllowDirty` is explicitly supplied, applies the pinned
MikroMCP Windows/audit overlays temporarily, and emits both a portable directory and a ZIP under
`dist\offline`. On the target machine run `install.ps1 -AddToPath` from the extracted bundle when
the `mth` command should be available in new PowerShell windows; no npm, Git,
PyPI, GitHub, or global Python/Node installation is used.
