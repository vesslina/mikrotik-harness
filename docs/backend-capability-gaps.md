# Pinned MikroMCP capability gaps

This file is the fail-closed boundary for MikroMCP `v1.9.0` at commit
`955cd99e9125b76d9ccc3f3c2f009a33de479a52`. A context-plan item is not called implemented merely
because a similarly named backend tool exists.

| Scenario | Exact gap | Harness decision |
| --- | --- | --- |
| Complete DHCP LAN | `manage_ip_pool` and `manage_dhcp_server` exist, but no typed mutation creates `/ip dhcp-server network` with gateway/DNS options. | `/dhcp` creates pool + server only after an operator confirms the matching network entry already exists. |
| Wi-Fi SSID/password | `manage_wifi_interface` can set SSID/disabled state but exposes no security profile or passphrase. It also lacks the snapshot metadata required by the generic journal workflow. | No Wi-Fi runbook. Do not claim password configuration or rollback safety. |
| Baseline firewall | `manage_firewall_rule` lacks important match fields such as connection state needed by a defensible baseline. Raw CLI would bypass the typed boundary; rollback also cannot restore rule order. | Keep read-only firewall inspection and NAT masquerade only. |
| Backup/export | `create_backup` and `export_config` are operational tools, not confirmation-token/journal-bound configuration changes. | Keep them outside the universal change runbook until an explicit artifact workflow and retention policy exist. |
| SSH port/restriction | `manage_ip_service` deliberately excludes port changes to prevent lockout. `run_command` is only a best-effort guarded SSH escape hatch. | Manual operation only; `run_command` remains outside model RBAC. |
| Wi-Fi validation on CHR | CHR has no physical radio or switch-chip behavior. | Validate Wi-Fi later on isolated physical hardware even after the typed backend gap is closed. |

Closing a row requires reviewing the exact new backend schema, snapshot paths, dry-run behavior,
idempotency, confirmation flow, and post-check fields. Do not unpin or locally fork MikroMCP only
to make a runbook appear complete; track an upstream change deliberately.

## Reviewed v1.9 compatibility overlay

MikroMCP v1.9's generic REST `update()` always builds `PATCH path/<id>`. RouterOS singleton menus
such as `/ip/dns` do not expose a `.id`, so `manage_dns_settings` reaches
`PATCH /rest/ip/dns/undefined` and fails with HTTP 500. mth leaves the submodule untouched and
creates an ignored copy of the built bundle named `dist/mth-main.js`. In that copy only, an update
with an ID remains a PATCH while an ID-less update uses RouterOS's typed `POST path/set` command.
The transformation matches one exact reviewed source fragment and refuses to start if a future
bundle differs. Remove this overlay after pinning an upstream release that implements singleton
updates itself.
