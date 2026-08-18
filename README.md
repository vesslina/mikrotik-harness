# MikroTik Harness

`mth` is a safety-oriented harness for managing RouterOS through a pinned MikroMCP backend.
It currently completes Block A and the first read-only foundation of Block B: MNDP discovery,
TLS trust-on-first-use, backend registration, health verification, dynamic tool discovery, and
system status.

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
- A successful connection shows the live tool count and RouterOS status in the UI.

Before connecting, enable RouterOS 7's HTTPS REST service from a trusted management path:

```routeros
/ip service enable api-ssl
/ip service set api-ssl port=443
```

Use a dedicated least-privilege RouterOS account with a non-empty password. MNDP values remain
untrusted self-announcements; only the pinned TLS connection establishes device continuity.

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
