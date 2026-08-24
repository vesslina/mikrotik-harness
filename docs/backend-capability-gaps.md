# Pinned MikroMCP capability gaps

This file is the fail-closed boundary for MikroMCP `v1.10.0` at commit
`aeee411fdf5acf4ae736800eb781d9b57b7660bb`. A context-plan item is not called implemented merely
because a similarly named backend tool exists.

| Scenario | Exact gap | Harness decision |
| --- | --- | --- |
| Read all static interface addresses | v1.10 still exposes `manage_ip_address`, but no dedicated upstream `list_ip_addresses` tool. | A narrow harness-owned read-only REST extension supplies `list_ip_addresses`; all writes remain inside MikroMCP. |
| Complete DHCP LAN | `manage_ip_pool` and `manage_dhcp_server` exist, but no typed mutation creates `/ip dhcp-server network` with gateway/DNS options. | `/dhcp` creates pool + server only after an operator confirms the matching network entry already exists. |
| Wi-Fi SSID/password | `manage_wifi_interface` can set SSID/disabled state but exposes no security profile or passphrase. It also lacks the snapshot metadata required by the generic journal workflow. | No Wi-Fi runbook. Do not claim password configuration or rollback safety. |
| Baseline firewall | `manage_firewall_rule` lacks important match fields such as connection state needed by a defensible opinionated baseline. | READY may propose individual schema-supported firewall, mangle, and address-list changes through the typed approval gateway, but mth does not claim these constitute the complete baseline template from the original plan. |
| Backup/export in READY | `create_backup` and `export_config` are operational tools, not confirmation-token/journal-bound configuration changes. | Keep them outside the universal READY runbook. HIGH RISK uses its separate pre-flight artifact workflow and retention policy. |
| SSH port/restriction | `manage_ip_service` deliberately excludes port changes to prevent lockout. `run_command` is only a best-effort guarded SSH escape hatch. | Manual operation only; `run_command` remains outside model RBAC. |
| Wi-Fi validation on CHR | CHR has no physical radio or switch-chip behavior. | Validate Wi-Fi later on isolated physical hardware even after the typed backend gap is closed. |

Closing a row requires reviewing the exact new backend schema, snapshot paths, dry-run behavior,
idempotency, confirmation flow, and post-check fields. Do not unpin or locally fork MikroMCP only
to make a runbook appear complete; track an upstream change deliberately.

## Reviewed v1.10 compatibility overlay

MikroMCP v1.10 implements singleton writes and rollback through RouterOS's typed `POST path/set`
commands upstream. The old generic singleton patch has therefore been removed. mth still creates
an ignored `dist/mth-main.js` copy to add `actual-interface` and `slave` to the upstream snapshot
runtime-field filter. The transformation matches the reviewed v1.10 field list and refuses to
start if a future bundle changes that insertion point.
