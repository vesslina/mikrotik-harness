# MikroTik Harness

<p align="center">
  <img src="pic-git.PNG" alt="MikroTik Harness" width="760">
</p>

<p align="center">
  A keyboard-first LLM harness for inspecting and operating MikroTik RouterOS.
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0-e05d44.svg" alt="version 0.1.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-007ec6.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB.svg" alt="Python 3.11 or newer">
  <img src="https://img.shields.io/badge/node-22%2B-339933.svg" alt="Node.js 22 or newer">
  <img src="https://img.shields.io/badge/RouterOS-7.x-293845.svg" alt="RouterOS 7.x">
  <img src="https://img.shields.io/badge/MCP-MikroMCP%20v1.9.0-6f42c1.svg" alt="MikroMCP v1.9.0">
  <img src="https://img.shields.io/badge/UI-Textual%208.x-5B3CC4.svg" alt="Textual 8.x">
</p>

[Русская версия](README_RU.md)

`mth` discovers MikroTik devices, registers a selected router through MikroMCP, and provides a
provider-neutral agent loop for inspecting and changing the connected device.

The project is built around three boundaries:

- MikroMCP is the typed RouterOS backend and the source of the live MCP tool catalog.
- `mth` owns discovery, trust, model integration, mode policy, runbooks, approvals, history,
  and the terminal UI.
- The LLM never chooses a router and never receives more authority than the active mode allows.

## Requirements

- Python 3.11+
- Node.js 22+
- RouterOS 7.x with HTTPS REST enabled through `www-ssl`
- A local model endpoint (LM Studio, Ollama, or `ai.local`) or another OpenAI-compatible endpoint

MikroMCP is included as a pinned git submodule at `v1.9.0`. The harness uses the official Python
MCP SDK over stdio and starts the Node.js backend as a child process. The upstream submodule is
not modified. At runtime, `mth` creates an ignored compatibility bundle for RouterOS singleton
updates such as `/ip/dns` and refuses to start if the pinned bundle no longer matches the reviewed
overlay.

## Development setup

```powershell
python -m venv .venv
git submodule update --init
npm --prefix external/mikromcp ci
npm --prefix external/mikromcp run build
.venv\Scripts\python -m pip install -e ".[dev]"
```

Run the application with:

```powershell
mth
```

The private runtime state is stored below `.mth/` and is ignored by Git. This includes MikroMCP
registration, pinned trust records, encrypted provider secrets, runbook history, and HIGH RISK
recovery artifacts.

## Discovery and registration

The Discovery screen listens for MNDP replies and presents the device MAC, addresses, identity,
RouterOS version, and board. A device can also be entered manually. The selected address, login,
and password are used to register the router with MikroMCP.

Registration verifies the HTTPS REST service, captures the TLS certificate fingerprint, writes the
MikroMCP `routers.yaml`/identity environment, fetches the live `tools/list` catalog, and confirms
`check_router_health` plus `get_system_status`. A later TLS fingerprint mismatch is a hard stop.

RouterOS REST is served by `www-ssl`, not `api-ssl`. A development CHR can be prepared with:

```routeros
/certificate add name=mth-ca common-name=mth-ca key-usage=key-cert-sign,crl-sign
/certificate sign mth-ca
/certificate add name=mth-https common-name=192.168.56.103 subject-alt-name=IP:192.168.56.103 key-usage=tls-server
/certificate sign mth-https ca=mth-ca
/ip service set www-ssl port=443 certificate=mth-https disabled=no
```

Use the router's stable management address and a dedicated non-empty RouterOS password.

## Agent modes

`Tab` cycles through `PLAN`, `READY`, and `HIGH RISK`.

### PLAN

PLAN is a read-only reconnaissance mode. The harness fetches the live MikroMCP catalog and gives
the model only router-bound tools marked read-only. The model may inspect RouterOS state and
explain it, but it cannot propose or execute a change.

### READY

READY is the normal change-management mode. The model receives the complete live read-only
catalog plus harness-owned proposal tools:

- nine reviewed scenario runbooks for PPPoE, bridges, IP addresses, address lists, DHCP core,
  DNS, NAT, administrative services, and WireGuard;
- typed proposals for the reviewed MikroMCP write schemas supported by the current harness
  workflow.

The underlying MikroMCP write tools are not passed directly to the model. Every proposal follows
the same lifecycle:

```text
proposal → typed form → live baseline → dry-run → human approval
→ MikroMCP confirmation → apply → post-check → journal/history
→ separately approved rollback when required
```

Secrets are collected by masked harness forms and injected only while assembling the approved
backend call. Plans, transcript events, model context, and history remain secret-free. The model
receives a short Russian completion report after a verified apply.

The live backend catalog is dynamic and is never hardcoded to a fixed tool count. A backend tool's
presence does not make it a supported READY operation: sensitive, non-rollbackable, or incomplete
schemas remain outside the supported READY contract until they have a reviewed harness workflow.

### HIGH RISK

HIGH RISK is an explicit elevated mode for open-ended RouterOS work. It keeps all READY tools and
adds the live MikroMCP catalog, direct write tools, and `ssh_exec` for one-line RouterOS CLI
commands. There is no per-command approval gate; the mode itself is the operator's explicit
elevation decision.

Before the composer is unlocked, the harness:

1. performs independent SSH host-key trust-on-first-use and rejects later mismatches;
2. asks MikroMCP to create a password-protected binary backup and a text export;
3. downloads both artifacts over SFTP on the same pinned SSH connection, verifies them, and stores
   them with a manifest under `.mth/high-risk-backups/<router-id>/`;
4. opens one persistent AsyncSSH PTY, negotiates the RouterOS terminal, and confirms `<SAFE>`
   Safe Mode.

Every CLI command is framed with a unique RouterOS marker, has bounded output and timeout handling,
and uses the same PTY so menu context and Safe Mode survive across calls. The RouterOS-compatible
transport keeps AsyncSSH application keepalives disabled; TCP, command framing, and explicit
session state provide the liveness boundary without closing a healthy RouterOS channel.

Leaving HIGH RISK requires an explicit choice to commit and exit, abort and roll back through Safe
Mode, or keep the session open. The harness never sends `/quit` while that decision is unresolved.
`/rollback` in HIGH RISK is reserved for a separately confirmed full pre-flight `.backup` restore,
which reboots the router and causes a short outage.

The HIGH RISK system prompt requires a seven-step cycle: understand, inspect, plan, sanity-check,
execute, verify, and report. Reasoning is kept in English for token efficiency; user-facing
conversation and reports are Russian. The dedicated RouterOS CLI RAG corpus is intentionally
deferred and is tracked in [high-risk-rag-todo.md](docs/high-risk-rag-todo.md).

## Models and chat

`/model` saves named presets for local models or arbitrary OpenAI-compatible endpoints. Provider
metadata is stored separately from API credentials. Credentials use the Windows user-protected
vault when available, with an encrypted Fernet fallback; environment-variable references remain
supported. `/models` selects or deletes saved presets.

The chat supports Russian/English UI selection, bounded in-process conversation memory, model
warm-up, streamed OpenAI-compatible responses with a toggleable thinking panel, normalized
reasoning/tool events, inline approval forms, session history, transcript copying, and command
hints after `/`.

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

## Checks

```powershell
pytest
ruff check .
mypy src
python -m mth --help
```

The backend boundary and known MikroMCP gaps are documented in
[`docs/backend-capability-gaps.md`](docs/backend-capability-gaps.md). The current Block B
architecture is in [`docs/block-b-architecture.md`](docs/block-b-architecture.md), and the
three-level live model prompts are in
[`docs/model-evaluation-prompts-ru.md`](docs/model-evaluation-prompts-ru.md).

## License

MikroTik Harness is released under the [MIT License](LICENSE).
