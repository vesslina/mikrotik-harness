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
