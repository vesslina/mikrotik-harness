# HIGH RISK mode

`HIGH RISK` is the third MikroTik Harness agent mode:

```text
PLAN → READY → HIGH RISK → PLAN
```

`Tab` moves through the sequence. Entering HIGH RISK never silently succeeds: the Harness first
checks the SSH host key, makes a local recovery point, opens one pinned SSH PTY, and confirms
RouterOS Safe Mode. Only then does it unlock the composer and hand the agent an `ssh_exec` tool.

## What the model receives

- every live MikroMCP tool, including direct write tools;
- the existing `propose_*` helpers from READY as optional previews that open the normal reviewed
  workflow;
- `ssh_exec(command, timeout_seconds, max_output_bytes)` for one RouterOS CLI line in the one
  persistent PTY.

There is no per-command approval gate for direct tools in this mode. Harness-owned `routerId` and
`confirmationToken` fields are hidden from the model; the harness binds the connected router and
completes MikroMCP's exact-argument confirmation handshake internally. The UI displays the mode in
red and writes a clear warning into the transcript. The HIGH RISK prompt requires the model to: understand the
request; inspect tools/state/CLI syntax; make a plan; quickly check it; apply only requested
work; quickly verify it; and report to the operator. Internal reasoning is English; operator
messages are Russian. The prompt forbids unrequested broad or irreversible work, but the Harness
does **not** hide commands behind a secret deny-list.

## Host-key TOFU

TLS trust for MikroMCP REST and SSH trust are independent. On the first SSH use, the Harness gets
the server key before authentication, shows its SHA-256 fingerprint, and waits for the operator's
confirmation. It stores the canonical OpenSSH public key in `.mth/mikromcp/ssh-hosts.yaml` and
the compatible raw SHA-256 fingerprint/port in the MikroMCP router entry. A later mismatch is a
hard stop; the UI never offers automatic replacement of a key.

## Pre-flight artifacts

Before the composer is unlocked, the Harness asks MikroMCP to create:

1. a password-encrypted binary `.backup`;
2. a textual `.rsc` export.

The randomly generated backup password is held only in the existing local encrypted secret vault.
The Harness opens the pinned SSH transport, uses its SFTP subsystem to download both remote files,
checks that each is non-empty and readable, hashes them, and writes a manifest beneath:

```text
.mth/high-risk-backups/<router-id>/<timestamp>/
```

The manifest carries hashes, identity and a secret reference — never the backup password. Any
failure aborts entry into HIGH RISK before the composer becomes available.

### Router prerequisites

The registered RouterOS account needs SSH access and the `ftp` policy because SFTP is the secure
transfer subsystem used to retrieve and restore artifacts. The SSH service must be reachable on
the configured port (22 by default); the Harness fails closed when it is unavailable.

## Persistent SSH and Safe Mode

The dedicated AsyncSSH channel is not MikroMCP's single guarded `run_command` call. It owns one
SSH connection and one PTY for the entire HIGH RISK session, so RouterOS menu context and Safe
Mode survive across model tool calls. Every command is framed with a unique
`:put "__MTH_CMD_DONE_<uuid>__"` marker. A timeout requests `Ctrl+C` and tries to re-synchronise;
an unrecoverable stream is closed rather than reused.

The Harness enters Safe Mode with `Ctrl+X` and verifies the `<SAFE>` prompt marker. It counts
commands and gives a non-blocking warning as it approaches the approximate 100-action Safe Mode
history limit. Do not change the router at the same time through WinBox or another login: Safe
Mode can roll back changes from those sessions as well.

Leaving HIGH RISK always opens an explicit inline choice:

- **Commit and exit**: release Safe Mode, verify its release, then close SSH.
- **Abort and roll back**: send `Ctrl+D`, then drop SSH so Safe Mode rolls the session back.
- **Keep HIGH RISK open**: leave the session untouched.

The Harness never sends `/quit` while a Safe Mode decision is unresolved.

## `/rollback` in HIGH RISK

`/rollback` has exactly one meaning here: full pre-flight `.backup` restore. It requires two
separate confirmations. The second plainly warns that RouterOS will reboot immediately and the
network/SSH connection will disappear. A dedicated restore flow uploads the original binary file
over SFTP and answers RouterOS' interactive reboot confirmation; generic `ssh_exec` is never used
for this operation.

## RAG status

The portable RouterOS corpus and lexical retrieval foundation are integrated through the local
read-only `search_routeros_docs` agent tool. Retrieved excerpts are bounded, source-linked and
marked as untrusted reference evidence; they never replace live inspection or verification. If no
pack is installed or retrieval is inconclusive, the agent uses CLI help. See
[rag-packs.md](rag-packs.md) and [high-risk-rag-todo.md](high-risk-rag-todo.md).
