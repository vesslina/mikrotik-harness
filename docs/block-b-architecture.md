# Block B architecture decision

## Current state

Block A is complete and the first major Block B pass is operational. A connected model can read
live RouterOS state through a bounded capability pack, explain the result, and hand a supported
change request to one of four deterministic runbooks. The model never executes a write itself.

Implemented runbooks:

| Command | Runbook | Reviewed write tools |
| --- | --- | --- |
| `/pppoe` | WAN PPPoE client | `manage_pppoe_client` |
| `/bridge` | LAN bridge and member ports | `manage_bridge`, `manage_bridge_port` |
| `/nat` | WAN source-NAT masquerade | `manage_firewall_rule` |
| `/services` | Disable a lockout-safe service subset | `manage_ip_service` |

The remaining catalog from the project context is intentionally not represented as direct model
tools. New change capability is added by registering another reviewed `RunbookDefinition`, not by
relaxing the agent boundary.

## Dependency and runtime

- Backend: `external/mikromcp`, git submodule pinned at tag `v1.9.0` (commit
  `955cd99e9125b76d9ccc3f3c2f009a33de479a52`).
- Client: official Python `mcp` package over stdio.
- Private configuration and execution history: `.mth/`; never committed.
- The initialized server's `tools/list` response is the runtime source of truth. The 122-tool
  count observed during development is diagnostic and is never hardcoded as a contract.

```text
Textual chat
  -> provider-neutral agent loop
       -> local capability selector
       -> filtered live MikroMCP read tools
       -> local propose_* handoff
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

PLAN mode is genuinely tool-free and does not start MikroMCP. READY initially exposes only
`select_router_capabilities`. The model selects up to three domains:

- overview;
- interfaces;
- addressing and services;
- firewall and routing;
- WAN and VPN;
- system;
- containers;
- diagnostics.

The harness then filters the live catalog to router-bound tools whose names are an approved read
shape (`list_*`, `get_*`, `check_router_health`, `ping`, or `traceroute`) and whose annotations
say `readOnlyHint=true` and not destructive. The selected domain receives only the relevant
subset plus matching local `propose_*` tools. A live development check covered all 60 eligible
read tools in the 122-tool catalog; individual packs contained 7–18 tools.

Every real call is rebound to the connected `routerId`. Fleet-global tools, management tools,
`apply_plan`, and `run_command` never reach the model. Device output is treated as untrusted data
and recursively redacted before it enters either model context or normalized UI events.

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
apply call. They are absent from proposal schemas, dry-run steps, transcript text, plans, and
history. This boundary is independent from the optional loopback-model setting that allows raw
sensitive read results to enter local model context.

RouterOS can include a `password` field in `list_pppoe_clients`. Baseline capture therefore does
not persist arbitrary records: every runbook projects only the small allowlist of fields required
for later verification. Arbitrary comments, scripts, credentials, and other device-controlled
fields cannot silently become durable runbook state.

The stdio process always uses a named `mth-operator` identity and a persistent confirmation
secret. Its RBAC allowlist is migrated from the registered runbook catalog and contains generic
plan/apply/rollback meta-tools plus only the five reviewed write tools listed above. Nested
`apply_plan` dispatch is checked again by the harness against the immutable definition.

## Runbook-specific limits

- PPPoE configuration verification and operational state are separate. A matching but disabled
  or disconnected client is verified configuration, not an active WAN session.
- A bridge plan supports at most nine member ports because MikroMCP plans support ten steps and
  bridge creation consumes one. Moving a management interface can interrupt connectivity and is
  called out before approval.
- NAT is source masquerade only. The pinned backend's typed firewall tool does not expose the
  destination-NAT translation fields required for a safe port-forward runbook.
- Firewall rollback restores presence and fields but cannot guarantee original rule order.
- The services runbook can disable only `api`, `api-ssl`, `telnet`, `www`, and `ftp`. It refuses
  `www-ssl`, `ssh`, and `winbox` to protect the harness and operator management paths.

## Next Block B passes

1. Add the remaining reviewed runbooks from the project catalog in coherent groups: DHCP/DNS,
   firewall baseline, Wi-Fi, WireGuard, backup/export, and diagnostics.
2. Add CHR golden tests for successful apply and rollback. PPPoE's active-session case needs a
   local PPPoE server; bridge/NAT/services should use isolated temporary objects.
3. Add optional provider streaming without changing normalized events or safety boundaries.
4. Add deterministic runbook-selected RAG and curated golden examples after the operational
   catalog is complete.

RAG remains deliberately deferred: typed live state and deterministic runbooks deliver more
operational value now, while documentation retrieval can be added later without reopening the
write boundary.
