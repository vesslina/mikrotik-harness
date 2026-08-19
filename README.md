# MikroTik Harness

`mth` is a safety-oriented harness for managing RouterOS through a pinned MikroMCP backend.
It completes Block A and now includes the first write-capable Block B runbook: MNDP discovery,
TLS trust-on-first-use, backend registration, model presets, a dynamic read-only MCP agent, and
an approval-bound WAN PPPoE workflow with verification and rollback.

## Development setup

```powershell
python -m venv .venv
git submodule update --init
npm --prefix external/mikromcp ci
npm --prefix external/mikromcp run build
.venv\Scripts\python -m pip install -e ".[dev]"
```

MikroMCP is pinned as a git submodule at `v1.9.0`; the harness never modifies its code. On a
machine where npm is unavailable, pnpm can install the same source dependencies and run its
local `node_modules/.bin/tsup` builder.

## Terminal UI

Launch the interactive interface:

```powershell
mth
```

The table is populated in the background. Use the arrow keys to select a device, `Tab` to move
through the connection fields, `r` to refresh, and `q` to quit. The password field is masked.

- Selecting a row copies its IP address into **Connect to**.
- **Connect** captures the RouterOS TLS certificate fingerprint. On first use it must be
  confirmed explicitly; later mismatches hard-stop before credentials are sent.
- Accepted devices are stored under `.mth/mikromcp/` using MikroMCP's native `routers.yaml`,
  `identities.yaml`, and `.env` formats. The whole `.mth/` directory is ignored by Git.
- The stdio backend runs as a scoped `operator` identity. It dynamically fetches `tools/list`,
  calls `check_router_health`, then calls the read-only `get_system_status` tool.
- A successful connection opens the chat screen with the live device profile and MCP tool count.

Before connecting, bootstrap RouterOS 7's HTTPS REST service from a trusted management path.
REST is served by `www-ssl`; `api-ssl` is a different, binary RouterOS API and must not be moved
to port 443. The following development setup creates a local certificate authority, signs a
server certificate for the router's management IP, and assigns it to `www-ssl`:

```routeros
/certificate add name=mth-ca common-name=mth-ca key-usage=key-cert-sign,crl-sign
/certificate sign mth-ca
/certificate add name=mth-https common-name=192.168.56.103 subject-alt-name=IP:192.168.56.103 key-usage=tls-server
/certificate sign mth-https ca=mth-ca
/ip service set www-ssl port=443 certificate=mth-https disabled=no
```

The explicit `ca=mth-ca` is required because `mth-https` has server-only key usage and cannot
self-sign. If the server template already exists after an earlier failed signing attempt, keep
it, create/sign `mth-ca`, and then run only `/certificate sign mth-https ca=mth-ca`.

Replace the IP with the router's stable management address. If `api-ssl` was previously moved
to port 443 while following an older revision of this README, restore it before enabling
`www-ssl`:

```routeros
/ip service set api-ssl port=8729 disabled=yes
```

`www-ssl` is normally disabled on an unconfigured router and HTTPS requires a certificate. This
one-time bootstrap is deliberately not attempted over plaintext HTTP: RouterOS REST uses Basic
authentication, so doing that would expose the management credentials. A future explicitly
opted-in SSH bootstrap may automate this step when an already trusted SSH path exists.

Use a dedicated least-privilege RouterOS account with a non-empty password. MNDP values remain
untrusted self-announcements; only the pinned TLS connection establishes device continuity.

## Agent chat

After a successful connection, `mth` opens a keyboard-first chat screen. Its pixel header keeps
the connected device, RouterOS version, selected model, harness version, and live MCP tool count
visible throughout the session.

- `/model` configures Local/LM Studio, OpenRouter, or a custom OpenAI-compatible endpoint.
- `/models` opens a keyboard-driven picker for every saved preset; `/models <name>` remains a
  direct activation shortcut.
- `/pppoe` opens the masked WAN PPPoE wizard in READY mode. It builds a live dry-run plan,
  requires an explicit human approval, applies through MikroMCP, and verifies the resulting
  interface.
- A natural-language request to add or configure WAN PPPoE can call the harness-owned
  `propose_wan_pppoe` handoff. It only opens the same editable masked wizard; it is not a
  backend write and cannot bypass dry-run, approval, confirmation, verification, or rollback.
- `/rollback [journal-id]` previews and confirms rollback of a PPPoE change created in the
  current session; omitting the ID selects the most recent eligible change.
- `/help`, `/info`, `/log`, `/clear`, and `/exit` provide the remaining command surface.
- Typing `/` shows matching commands below the composer; a unique prefix can be completed with
  `Tab`.
- `Tab` cycles between `PLAN` and `READY`. PLAN exposes no tools. Normal READY chat exposes only
  read-only, router-bound tools; writes are reachable only through reviewed deterministic
  runbooks and their approval UI. Fleet-global tools and `run_command` remain excluded.

An API key entered in `/model` remains in process memory only. Presets under
`.mth/providers.json` store endpoint/model/capability metadata and optionally an environment
variable name, never the key value itself. The VS Code setting `python.terminal.useEnvFile` is
not required by `mth`; RouterOS credentials are passed directly to the pinned child process.

MCP tool results are recursively redacted before both the transcript event and the next LLM
request. Credential-shaped fields such as passwords, API keys, private keys, communities, and
tokens are protected by default, including values nested inside `structuredContent`. A model
preset may explicitly expose those fields only when its URL resolves syntactically to a
loopback endpoint (`localhost`, `127.0.0.0/8`, or `::1`); non-loopback presets fail validation.
This local privacy override is off by default and is visibly announced when active.

Selecting a model triggers a hidden tool-free warm-up probe. Success reports latency; connection,
authentication, model-name, and malformed-response failures retain distinct error codes.

LM Studio reasoning models may return a non-standard `reasoning_content` field while leaving the
OpenAI-compatible `message.content` empty. The harness recognizes both `reasoning_content` and
`reasoning`, shows a compact reasoning-status line without dumping hidden reasoning, and recovers
only an explicitly labelled final-answer section when the provider misplaced it there.

The model never chooses a router ID. Every backend MCP call is rebound to the currently
connected router, and RouterOS/device output is framed as untrusted data rather than
instructions.

## Headless discovery

Run an active MNDP probe and listen for replies for three seconds:

```powershell
mth discover
mth discover --json
```

If limited broadcast routing is ambiguous on a host with multiple adapters, provide the directed
broadcast address explicitly. Normally this is unnecessary: `mth` binds a sender to each local
IPv4 address so a limited broadcast reaches every adapter.

```powershell
mth discover --broadcast 192.168.56.255
```

MAC-only RouterOS connections are outside the v1 scope.

## Checks

```powershell
pytest
ruff check .
mypy src
python -m mth --help
```

The Block B architecture and next implementation slices are recorded in
[`docs/block-b-architecture.md`](docs/block-b-architecture.md).
