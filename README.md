# MikroTik Harness

<p align="center">
  <img src="pic-git.PNG" alt="MikroTik Harness" width="760">
</p>

<p align="center">
  A keyboard-first RouterOS workspace for LLM agents.
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0-e05d44.svg" alt="version 0.1.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-007ec6.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6.svg" alt="Windows 10 or 11">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB.svg" alt="Python 3.11 or 3.12">
  <img src="https://img.shields.io/badge/node-22%2B-339933.svg" alt="Node.js 22 or newer">
  <img src="https://img.shields.io/badge/RouterOS-7.x-293845.svg" alt="RouterOS 7.x">
  <img src="https://img.shields.io/badge/backend-MikroMCP%20v1.10.0-6f42c1.svg" alt="MikroMCP v1.10.0">
</p>

[Русская версия](README_RU.md)

MikroTik Harness (`mth`) is a community CLI project that gives an LLM agent a practical,
mode-controlled workspace for MikroTik RouterOS. It discovers a router, connects a model, exposes
the appropriate tools, records the session, and keeps the operator in control of changes.

The typed RouterOS backend comes from
[MikroMCP](https://github.com/AliKarami/MikroMCP) ([official site](https://mikromcp.com/)).
`mth` runs the pinned backend locally and adds discovery, model providers, permissions, approvals,
session history, offline RouterOS reference search, and a persistent HIGH RISK SSH channel.

> [!WARNING]
> This is not an official MikroTik product. Test changes on a lab device first. HIGH RISK can make
> any change the connected RouterOS user is allowed to make.

## What it does

- Discovers MikroTik devices over MNDP or connects to a manually entered address.
- Works with LM Studio, Ollama, and arbitrary OpenAI-compatible chat-completions endpoints.
- Gives the model different RouterOS authority in PLAN, READY, and HIGH RISK modes.
- Uses the live MikroMCP catalog instead of assuming a fixed tool count.
- Shows tool calls, reasoning, approvals, verification, and the final report in one terminal UI.
- Stores model presets and chat sessions locally; provider secrets use the Windows user vault.
- Searches a portable local copy of the official RouterOS manual without an embedding model.
- Creates a pre-flight backup and enters RouterOS Safe Mode before HIGH RISK is unlocked.

## Agent modes

Press `Tab` to cycle between modes.

| Mode | Agent access | Intended use |
| --- | --- | --- |
| **PLAN** | Live read-only MikroMCP tools | Inventory, diagnostics, and planning without changes |
| **READY** | Read tools plus reviewed proposal/runbook workflows | Normal changes with preview, approval, apply, and verification |
| **HIGH RISK** | READY tools, the live MikroMCP catalog, and persistent RouterOS CLI over SSH | Open-ended engineering when a reviewed workflow is not enough |

READY does not hand every raw write call directly to the model. A supported proposal is checked
against live state, shown to the operator, approved, applied through MikroMCP, and verified.

HIGH RISK deliberately removes that restriction. Before it opens, `mth` pins the SSH host key,
creates and downloads a binary backup plus text export, verifies both artifacts, opens one
persistent AsyncSSH PTY, and confirms RouterOS Safe Mode. The same channel is kept for every
`ssh_exec` call so CLI context and Safe Mode survive between commands. Leaving the mode requires
an explicit commit or Safe Mode rollback decision. `/rollback` performs a separately confirmed
full backup restore and reboots the router.

## Requirements

Current source installation requires:

- 64-bit Windows 10 or Windows 11;
- CPython 3.11 or 3.12;
- Node.js 22 or newer and npm;
- Git for the source checkout and MikroMCP submodule;
- RouterOS 7.x with HTTPS REST (`www-ssl`); SSH is also required for HIGH RISK;
- an LLM endpoint: LM Studio, Ollama, or another OpenAI-compatible provider.

PowerShell 7, Visual Studio, C++ Build Tools, and a separate Microsoft VC++ Redistributable are not
source-install requirements when pip installs the published binary wheels. Windows PowerShell 5.1
is sufficient for the setup commands below.

## Install from source on Windows

Clone recursively: a plain `git clone` does **not** populate the MikroMCP submodule.

```powershell
git clone --recurse-submodules https://github.com/vesslina/mikrotik-harness.git
cd mikrotik-harness

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
npm --prefix external/mikromcp ci
npm --prefix external/mikromcp run build
.\.venv\Scripts\python.exe -m pip install -e .
```

Start it directly:

```powershell
.\.venv\Scripts\mth.exe
```

Or activate the virtual environment first; then `mth` works as a normal command:

```powershell
.\.venv\Scripts\Activate.ps1
mth
```

For an existing non-recursive checkout, run:

```powershell
git submodule update --init --recursive
```

## Prepare RouterOS once

MikroMCP uses RouterOS HTTPS REST through `www-ssl`; `api-ssl` is a different service. Replace both
instances of `<ROUTER_IP>` with the router's stable management address before pasting these commands:

```routeros
/certificate add name=mth-ca common-name=mth-ca key-usage=key-cert-sign,crl-sign
/certificate sign mth-ca
/certificate add name=mth-https common-name=<ROUTER_IP> subject-alt-name=IP:<ROUTER_IP> key-usage=tls-server
/certificate sign mth-https ca=mth-ca
/ip service set www-ssl port=443 certificate=mth-https disabled=no
/ip service set ssh disabled=no
```

Use a RouterOS account with a non-empty password and only the permissions required for your work.
The first connection displays the TLS fingerprint. HIGH RISK separately displays and pins the SSH
host-key fingerprint. A later fingerprint mismatch is a hard stop, not a warning to ignore.

## First session

1. Run `mth` and select the router in Discovery, or enter its management address manually.
2. Enter the RouterOS login and password and approve the first TLS fingerprint after verifying it.
3. Run `/model`, choose **Local model** or **OpenAI-compatible provider**, and save a preset.
4. Ask the agent to inspect the device in PLAN. Press `Tab` only when the requested authority is
   appropriate.

For Ollama, start `ollama serve`, pull a model with tool-call support, and use
`http://127.0.0.1:11434/v1`. For LM Studio, enable its local OpenAI-compatible server and use the URL
shown by LM Studio. Ollama and LM Studio themselves are not bundled with `mth`.

## Offline RouterOS manual

Build the documentation pack once on a connected workstation:

```powershell
mth rag
mth rag --query "safe mode rollback"
```

The default pack is `.mth/rag`. Copy that complete directory to the same location on an offline
machine, or point `MTH_RAG_HOME` at it. A populated pack is checksum-validated and opened without a
network request. Search uses the standard-library SQLite FTS5 index, so it needs neither Chroma nor
an embedding model. URLs displayed beside results are source attribution stored in the local index;
the agent does not open them.

For a released corpus, keep its external `<rag-directory>.sha256` sidecar beside the pack (or set
`MTH_RAG_CHECKSUM`). It pins `manifest.json`; the manifest pins the database, index, and source
pages. `mth rag --checksum <sidecar>` verifies the same chain explicitly.

Project-owned field recipes live in [`docs/field-recipes`](docs/field-recipes). Adding a Markdown
card there makes it available to `search_field_recipes` without downloading anything.

The MikroTik documentation corpus is not redistributed by this public repository. Build it from
the official source or transfer your own validated copy under the applicable documentation terms.

## Commands

```text
/help       command reference
/info       connected router and model details
/model      add or edit a model preset
/models     select or delete a saved preset
/language   select English or Russian
/new        start a new chat session
/history    list saved sessions
/resume     resume the latest session
/log        show the local audit transcript
/clear      clear transcript and model memory
/rollback   preview and confirm an eligible rollback
/exit       leave the chat
```

Headless discovery is also available:

```powershell
mth discover
mth discover --json
mth discover --broadcast 192.168.56.255
```

## Local data

Private state is stored below `.mth/` and ignored by Git: router registration, pinned trust
records, encrypted provider secrets, runbook history, chat sessions, HIGH RISK recovery artifacts,
and the optional manual pack. Do not publish this directory.

## Offline field deployment

Copying a development virtual environment to another PC is not supported: Windows venv launchers
contain machine-specific paths. The 1.0 release target is a per-user offline bundle containing a
private CPython runtime, private Node.js runtime, prebuilt MikroMCP, a Python wheelhouse, and an
optional operator-supplied RAG pack. The target laptop will need no Git, npm, global Python/Node,
administrator rights, or internet connection.

The frozen bundle layout, install flow, dependency inventory, and clean-machine acceptance matrix
are documented in [Windows offline distribution](docs/windows-offline-distribution.md). Until that
bundle passes the Python 3.11 and 3.12 clean-machine matrix, source installation above is the only
supported installation method.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\python.exe -m mth --help
```

Architecture and safety details are in
[`docs/block-b-architecture.md`](docs/block-b-architecture.md),
[`docs/high-risk-mode.md`](docs/high-risk-mode.md), and
[`docs/rag-packs.md`](docs/rag-packs.md).

## License

MikroTik Harness is released under the [MIT License](LICENSE). MikroMCP and third-party components
retain their own licenses.
