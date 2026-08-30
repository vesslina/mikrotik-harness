# Architecture

MikroTik Harness is a Windows-first terminal application around a pinned MikroMCP backend. It
keeps model access, RouterOS authority, local state, and recovery mechanisms in one process while
leaving the operator in control of mode changes and approvals.

```text
Textual UI
  -> provider-neutral agent loop
       -> live MikroMCP catalog over stdio
       -> RouterOS HTTPS REST for typed tools
       -> reviewed READY runbooks and typed proposals
       -> persistent AsyncSSH PTY in HIGH RISK
       -> local SQLite FTS5 documentation search
```

The Python package owns discovery, registration, trust-on-first-use, agent routing, approvals,
session history, RAG, and HIGH RISK SSH. `external/mikromcp` is a pinned git submodule and remains
the typed RouterOS backend. Application code does not patch its source checkout at runtime; a
harness-owned compatibility entrypoint is generated beside the built upstream bundle.

## Authority modes

| Mode | Model-visible authority |
| --- | --- |
| PLAN | Router-bound tools annotated read-only by the live MikroMCP catalog |
| READY | PLAN tools plus reviewed scenario runbooks and schema-derived typed proposals |
| HIGH RISK | Live MikroMCP tools plus one persistent RouterOS CLI channel over SSH |

The initialized `tools/list` response is the source of truth. Historical tool counts are
diagnostic only and never form an authorization rule. Harness-owned `routerId` and
`confirmationToken` parameters are hidden from the model and rebound by the application.

PLAN filters out destructive or non-read-only tools. READY never exposes raw `manage_*` calls:
the nine built-in scenarios cover PPPoE, bridge, IP address, address list, DHCP, DNS, source NAT,
management services, and WireGuard. A reviewed typed allowlist adds proposals for safe backend
schemas which do not contain credentials or arbitrary command/script payloads.

HIGH RISK intentionally removes the READY write boundary. Its pre-flight backup, SSH host-key
pinning, persistent PTY, Safe Mode lifecycle, commit/abort choice, and full-backup restore are
specified in [high-risk-mode.md](high-risk-mode.md).

## READY change lifecycle

Every READY change follows one executor path:

1. parse public fields and keep secrets separate;
2. capture a projected live baseline;
3. build a bounded deterministic plan and run MikroMCP dry-run;
4. show the immutable plan to the operator;
5. obtain the backend confirmation challenge after approval;
6. apply each step and retain its rollback journal;
7. verify the resulting live state;
8. persist a secret-free execution record;
9. optionally ask the provider for a short user-facing report.

Rollback is separately previewed and approved. Journals run in reverse order and the saved
baseline is checked again. A failed optional reporting call cannot change or hide the backend
result.

## Trust and local state

MNDP announcements are untrusted discovery hints. Registration captures the RouterOS TLS leaf
fingerprint and MikroMCP enforces that pin for HTTPS REST. HIGH RISK maintains an independent SSH
host-key pin. A later mismatch fails closed.

Private state lives under `.mth/` and is excluded from Git. It includes router registration,
encrypted provider secrets, model presets, chat sessions, runbook history, trust records, RAG,
and recovery artifacts. Windows DPAPI is preferred for secrets; a private encrypted file fallback
supports portable environments. Device and tool output is recursively redacted before it enters
remote model context unless the operator explicitly enables the loopback-only local-model privacy
override.

## Retrieval

HIGH RISK can search a portable, checksummed local copy of the RouterOS manual through
`search_routeros_docs`. Project-owned device recipes use the separate `search_field_recipes`
collection. Both are read-only evidence: neither authorizes a command or replaces live
verification. Pack format, downloader trust boundaries, offline behavior, and recipe schema are
specified in [rag-packs.md](rag-packs.md).

PLAN and READY currently rely on live schemas and deterministic runbooks rather than a separate
operational RAG corpus. Adding such a corpus requires reviewed field guidance; it is not simulated
with generic documentation.

## Distribution boundary

Source installs support CPython 3.11/3.12 and Node.js 22+. The offline field bundle contains a
wheelhouse, Node ZIP, Python installer, built MikroMCP runtime, checksums, licenses, and optional
operator-owned RAG data. The target machine performs no Git, npm, PyPI, or documentation download.
See [windows-offline-distribution.md](windows-offline-distribution.md).

## Open-beta limits

- READY covers reviewed changes, not every MikroMCP write schema.
- Raw scripts, arbitrary commands, reboot/upgrade, broad file writes, and irreversible operations
  remain HIGH RISK work.
- The public repository does not redistribute the RouterOS manual corpus.
- The current field-tested offline artifact uses CPython 3.11; CPython 3.12 remains source/CI
  supported and requires its own clean-machine bundle acceptance run.
