# MikroTik Harness

[Русская версия](README_RU.md)

`mth` is a safety-oriented harness for managing RouterOS through a pinned MikroMCP backend.
It completes Block A and now has a working Block B agent loop: MNDP discovery, TLS
trust-on-first-use, backend registration, model presets, capability-routed read tools, and seven
approval-bound runbooks with dry-run, verification, persistent history, and rollback.

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
visible throughout the session. The two-line logo uses an offset terminal shadow inspired by the
classic Claude Code wordmark while retaining the harness's black, white, and red identity.

- `/model` configures Local/LM Studio, OpenRouter, or a custom OpenAI-compatible endpoint.
- `/models` opens a keyboard-driven picker for every saved preset. The selected preset and its
  encrypted API key can be deleted there; `/models <name>` remains a direct activation shortcut.
- `/language` opens the inline English/Russian selector; `/language en` and `/language ru` switch
  directly. The first launch follows the operating-system locale and the choice is persisted.
- `/pppoe`, `/bridge`, `/dhcp`, `/dns`, `/nat`, `/services`, and `/wireguard` open schema-driven
  runbook wizards in READY mode. DHCP currently creates the pool and server only after the
  operator confirms that the matching RouterOS network/gateway entry already exists.
- Natural-language change requests can call the matching harness-owned `propose_*` handoff.
  These calls only open an editable form; the model never receives a backend write tool and
  cannot bypass dry-run, human approval, MikroMCP confirmation, post-check, or rollback.
- `/rollback [execution-id|journal-id]` previews and confirms rollback of the complete runbook.
  History is stored without secrets under `.mth/runbook-history.json`, so rollback still works
  after restarting `mth`. Omitting the ID selects the most recent eligible execution.
- `/help`, `/info`, `/log`, `/clear`, and `/exit` provide the remaining command surface. `/clear`
  clears both the visible transcript and the model's in-process conversation memory.
- Typing `/` shows matching commands below the composer; a unique prefix can be completed with
  `Tab`.
- Model setup, saved-model selection, runbook forms, deletion, apply, rollback, and language
  selection are inline interactions: they temporarily replace the composer without covering the
  transcript. `Esc` cancels and `Tab` returns an approval to editable parameters.
- `Tab` cycles between `PLAN` and `READY`. PLAN does not start MikroMCP or expose tools. READY
  first exposes one local capability selector; the model then receives only the small live
  read-only domain pack relevant to the request. Writes are reachable only through reviewed
  deterministic runbooks and their approval UI. Fleet-global tools and `run_command` remain
  excluded.

An API key entered in `/model` is saved separately from preset metadata in the encrypted
`.mth/provider-secrets.json` vault. Windows current-user DPAPI is preferred; if DPAPI is not
available, a Fernet key stored in the private `.mth/provider-secrets.key` file is used. Base64 is
only the serialization of encrypted bytes, never the encryption itself. A named environment
variable, when configured and non-empty, overrides the saved value. Secrets are never written to
`.mth/providers.json`, the transcript, logs, plans, or Git.

Conversation memory is bounded by the selected preset's declared context size and retains only
recent complete user/assistant turns. It is intentionally process-local and excludes raw hidden
reasoning. Increasing a preset's context setting now has an actual effect; recreate an incorrectly
sized preset after deleting it from `/models`.

User prompts are rendered on a grey transcript card while model output remains on the black
background. During a request an activity line shows a rotating status phrase and live elapsed
time. Exact reasoning-token usage appears when the provider returns it; non-streaming compatible
providers cannot report a trustworthy live token counter, so the active line shows an ellipsis
instead of inventing one. Tool actions appear as they happen between model rounds; `Ctrl+O`
expands the current turn's bound tool names and arguments.

MCP tool results are recursively redacted before both the transcript event and the next LLM
request. Credential-shaped fields such as passwords, API keys, private keys, communities, and
tokens are protected by default, including values nested inside `structuredContent`. A model
preset may explicitly expose those fields only when its URL resolves syntactically to a
loopback endpoint (`localhost`, `127.0.0.0/8`, or `::1`); non-loopback presets fail validation.
This local privacy override is off by default and is visibly announced when active.

Runbook baselines use an independent allowlist projection before they are persisted. For
example, RouterOS may return a `password` field from `list_pppoe_clients`; that field is never
copied into the plan or runbook history, even when a loopback model privacy override is active.

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
[`docs/block-b-architecture.md`](docs/block-b-architecture.md). The exact pinned-backend limits
are tracked in [`docs/backend-capability-gaps.md`](docs/backend-capability-gaps.md), and the
three-level live model test suite is in
[`docs/model-evaluation-prompts-ru.md`](docs/model-evaluation-prompts-ru.md).
