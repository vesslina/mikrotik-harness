# Block B architecture decision

## Current state

Block A is complete and the third major Block B pass is operational. A connected model can read
live RouterOS state in PLAN, use approval-bound changes in READY, and work through the dedicated
CLI channel in HIGH RISK. READY has nine deterministic scenario runbooks; the model never receives
their underlying write tools directly.

Implemented runbooks after Pass 2:

| Command | Runbook | Reviewed write tools |
| --- | --- | --- |
| `/pppoe` | WAN PPPoE client | `manage_pppoe_client` |
| `/bridge` | LAN bridge and member ports | `manage_bridge`, `manage_bridge_port` |
| `/ip-address` | Add an IPv4 address to an interface | `manage_ip_address` |
| `/address-list` | Add a firewall address-list entry | `manage_address_list_entry` |
| `/dhcp` | IP pool and DHCP server core | `manage_ip_pool`, `manage_dhcp_server` |
| `/dns` | RouterOS DNS resolver | `manage_dns_settings` |
| `/nat` | WAN source-NAT masquerade | `manage_firewall_rule` |
| `/services` | Disable a lockout-safe service subset | `manage_ip_service` |
| `/wireguard` | WireGuard interface and one peer | `manage_wireguard_interface`, `manage_wireguard_peer` |

The rest of the context catalog is intentionally not represented as direct model tools where the
pinned backend cannot express the complete operation safely. New scenario capability is added by
registering another reviewed `RunbookDefinition`; generic capability requires an explicit review
and allowlist entry, not a relaxation of the agent boundary.

READY additionally exposes a runtime typed-change layer for the reviewed MikroMCP write schemas.
For each selected capability domain, mth intersects the live catalog with the 27-entry reviewed
allowlist, rejects schemas containing credential fields, copies the remaining JSON schema into a
local `propose_typed_*` tool, removes harness-owned fields, and routes the proposal through the
same dry-run, confirmation-token, snapshot, journal, verification, history, and rollback
lifecycle. The underlying `manage_*` tool is never given to the model.

Raw commands, reboot/upgrade, scripts and scheduler payloads, file writes, container lifecycle,
SwOS blobs, and tools containing passwords/private secrets require dedicated workflows with
stronger UI and verification. A broad approval button is not sufficient authorization for
arbitrary code or an irreversible operation.

## Dependency and runtime

- Backend: `external/mikromcp`, git submodule pinned at tag `v1.10.0` (commit
  `aeee411fdf5acf4ae736800eb781d9b57b7660bb`).
- Client: official Python `mcp` package over stdio.
- Private configuration and execution history: `.mth/`; never committed.
- The initialized server's `tools/list` response is the runtime source of truth. The 122-tool
  count observed during development is diagnostic and is never hardcoded as a contract.

```text
Textual chat
  -> provider-neutral agent loop
       -> live read-only MikroMCP tools in PLAN/READY
       -> local scenario propose_* or schema-derived propose_typed_* handoff
            -> schema-driven runbook wizard
            -> RunbookExecutor
                 -> capture projected baseline
                 -> plan_changes (dry-run)
                 -> human approval
                 -> apply_plan confirmation challenge
                 -> apply + post-check
                 -> secret-free persistent history
                 -> previewed, approved, verified rollback
```

`core` never imports Textual. The UI creates definitions and executors through protocols and
factories; a runbook definition owns its parameter schema, deterministic step builder, baseline,
apply verification, rollback verification, capability domains, and rollback caveat.

## Agent tool routing

PLAN mode starts the live MCP catalog and exposes all router-bound read-only tools. READY exposes
the same read surface plus scenario and typed proposal tools. The capability selector remains a
catalog-routing helper for focused flows; it is not the authority boundary:

- overview;
- interfaces;
- addressing and services;
- firewall and routing;
- WAN and VPN;
- system;
- containers;
- diagnostics.

The harness filters the live catalog to router-bound tools whose annotations say
`readOnlyHint=true` and not destructive. The current CHR catalog contains 63 router-scoped read
tools plus one harness-owned address reader; the live count is never hardcoded.

`ToolCatalogRouter.ready_contract()` derives the machine-checkable boundary from that same live
catalog. It records PLAN/READY reads, scenario and typed writes, missing runbook dependencies,
uncovered backend writes, and accidental raw-write exposure. Tests can therefore fail closed on
drift without turning a historical tool count into policy.

Every model-facing schema hides harness-owned `routerId` and `confirmationToken` fields. Every real
call is rebound to the connected `routerId`; HIGH RISK direct destructive calls complete the
MikroMCP confirmation challenge internally with identical arguments. Fleet-global tools, management tools,
`apply_plan`, and `run_command` never reach the model. Device output is treated as untrusted data
and recursively redacted before it enters either model context or normalized UI events.

After apply and deterministic verification, the provider is called once more with tools disabled.
It receives only the approved plan summary, verified/unverified status, operational status, and
whether a rollback journal exists, then produces a 2–5 sentence user-facing report. Failure of
this optional reporting call cannot change or hide the backend result.

## Universal runbook lifecycle

`RunbookExecutor` enforces one lifecycle for every definition:

1. parse and validate typed public fields while separating secret fields;
2. build at most ten deterministic steps using only the definition's declared write tools;
3. capture a pre-change baseline and fail closed if it cannot be read;
4. send the secret-free steps to `plan_changes` and reject any nested `would_fail` result;
5. show the immutable plan to the operator;
6. rebuild apply arguments and allow differences only for declared secret backend parameters;
7. require MikroMCP's `CONFIRMATION_REQUIRED` challenge and single-use token;
8. require one rollback journal per successfully applied step;
9. run a definition-specific post-check in the same backend session;
10. persist the plan and journals even when apply is partial or the post-check is inconclusive;
11. roll journals back in reverse order after a separate preview and approval, then verify the
    result against the saved baseline.

The history file accepts either a runbook execution ID or any of its journal IDs. This makes a
multi-step bridge rollback atomic from the operator's point of view and restart-safe from the
harness's point of view.

## Secrets and untrusted state

PPPoE passwords are collected in a masked field and injected only while assembling the approved
specialized runbook call. They are absent from specialized proposal schemas, dry-run steps,
transcript text, plans, and history. Generic typed schemas are limited to the reviewed allowlist
and reject credential-shaped fields before they reach the model. This is independent from the
optional loopback-model setting that allows raw sensitive read results to enter local model
context.

Provider API keys use a separate encrypted vault. Current-user DPAPI is preferred on Windows; a
private Fernet key file is the portable fallback. The ciphertext is Base64-encoded for JSON, but
Base64 itself is not treated as encryption. Environment-variable references remain supported and
take precedence. Deleting a preset also deletes its encrypted vault entry.

RouterOS can include a `password` field in `list_pppoe_clients`. Baseline capture therefore does
not persist arbitrary records: every runbook projects only the small allowlist of fields required
for later verification. Arbitrary comments, scripts, credentials, and other device-controlled
fields cannot silently become durable runbook state.

The stdio process always uses a named `mth-operator` identity and a persistent confirmation
secret. Its RBAC policy is migrated from the registered catalog and is checked again by the
harness before nested `apply_plan` dispatch. Narrowing the backend patterns to the reviewed write
allowlist remains part of READY hardening.

The model loop retains a bounded window of recent complete turns. The budget comes from the
selected preset's context declaration with space reserved for the response. Tool traces are not
blindly persisted as dialogue; the assistant's final summary is retained and live state must be
read again before later changes. `/clear` clears this memory as well as the screen.

## Runbook-specific limits

- PPPoE configuration verification and operational state are separate. A matching but disabled
  or disconnected client is verified configuration, not an active WAN session.
- A bridge plan supports at most nine member ports because MikroMCP plans support ten steps and
  bridge creation consumes one. Moving a management interface can interrupt connectivity and is
  called out before approval.
- NAT is source masquerade only. The pinned backend's typed firewall tool does not expose the
  destination-NAT translation fields required for a safe port-forward runbook.
- Firewall rollback restores presence and fields but cannot guarantee original rule order.
- DHCP creates only an IP pool and server. The pinned backend has no typed DHCP network/gateway
  mutation, so an operator-only checkbox confirms that matching state already exists.
- WireGuard creates an interface and one peer. RouterOS generates the local private key; the
  model and runbook never accept it as input or persist it in the baseline.
- The services runbook can disable only `api`, `api-ssl`, `telnet`, `www`, and `ftp`. It refuses
  `www-ssl`, `ssh`, and `winbox` to protect the harness and operator management paths.
- Wi-Fi password, baseline firewall, backup/export, and SSH port mutation remain blocked for the
  exact reasons recorded in `docs/backend-capability-gaps.md`.

## Next Block B passes

1. Resolve or upstream the typed backend gaps for DHCP network entries, Wi-Fi security profiles,
   baseline firewall match fields, and rollback-bound backup/export.
2. Add CHR golden tests for successful apply and rollback. PPPoE's active-session case needs a
   local PPPoE server; bridge/NAT/services should use isolated temporary objects.
3. Add diagnostics/internet golden paths and additional reviewed field cards. The separate local
   RAG 2B collection and deterministic field-card injection after capability selection are now
   connected; source-provided RouterOS version metadata remains unavailable.
4. Add dense retrieval only if those evaluations show a material lexical-search gap.

The portable pack foundation is implemented with local Markdown, a checksummed manifest, and
SQLite FTS5. HIGH RISK exposes bounded results through a local read-only search tool; it remains
evidence rather than an authority boundary. See `docs/rag-packs.md`.
