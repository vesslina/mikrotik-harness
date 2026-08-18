# MikroTik Harness

`mth` is a safety-oriented harness for managing RouterOS through typed runbooks. The current
implementation covers the discovery and terminal-UI slices of Block A: discovering MikroTik
devices in the local Layer 2 broadcast domain with MNDP and selecting a connection target.

## Development setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## Terminal UI

Launch the interactive interface:

```powershell
mth
```

The table is populated in the background. Use the arrow keys to select a device, `Tab` to move
through the connection fields, `r` to refresh, and `q` to quit. The password field is masked.
Selecting and validating a connection target is implemented; MikroMCP registration is the next
Block A slice and the UI does not report a connection before that backend exists.

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

Discovery is not authentication. All displayed identity, version, board, address, and MAC fields
are untrusted self-announcements. MAC-only RouterOS connections are outside the v1 scope.

## Checks

```powershell
pytest
ruff check .
mypy src
python -m mth --help
```
