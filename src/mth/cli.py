import argparse
import json
import sys
from collections.abc import Sequence

from mth import __version__
from mth.core.discovery import DiscoveryError, DiscoveryErrorCode, discover_devices
from mth.core.discovery.models import DeviceInfo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mth",
        description=(
            "Discover MikroTik devices in the local Layer 2 broadcast domain using MNDP. "
            "Discovery data is an untrusted self-announcement, not authentication."
        ),
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("discover",),
        default="discover",
        help="command to run (default: discover)",
    )
    parser.add_argument("--timeout", type=float, default=3.0, help="listen duration in seconds")
    parser.add_argument(
        "--broadcast",
        action="append",
        dest="broadcasts",
        metavar="IP",
        help=(
            "broadcast destination; repeat for multiple adapters "
            "(default: auto-detect local IPv4 adapters)"
        ),
    )
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        dest="bind_address",
        metavar="IP",
        help="local IPv4 address to bind (default: 0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=5678, help="MNDP UDP port")
    parser.add_argument(
        "--listen-only",
        action="store_true",
        help="do not send an active four-byte MNDP probe",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit JSON")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    broadcasts = tuple(args.broadcasts) if args.broadcasts else None

    try:
        result = discover_devices(
            timeout=args.timeout,
            bind_address=args.bind_address,
            broadcasts=broadcasts,
            port=args.port,
            active=not args.listen_only,
        )
    except (ValueError, DiscoveryError) as error:
        code = error.code if isinstance(error, DiscoveryError) else DiscoveryErrorCode.SOCKET_ERROR
        _emit_error(code, str(error), as_json=args.as_json)
        return 3

    if not result.devices:
        _emit_error(
            DiscoveryErrorCode.TIMEOUT,
            f"No MNDP devices replied within {args.timeout:g} seconds",
            as_json=args.as_json,
        )
        return 2

    if args.as_json:
        print(
            json.dumps(
                {
                    "devices": [device.to_dict() for device in result.devices],
                    "warnings": list(result.warnings),
                },
                indent=2,
            )
        )
    else:
        print(_format_table(result.devices))
        for warning in result.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        print("\nMNDP fields are untrusted until the connection step verifies the device.")
    return 0


def _emit_error(code: DiscoveryErrorCode, message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": {"code": code, "message": message}}, indent=2))
    else:
        print(f"{code}: {message}", file=sys.stderr)

def _format_table(devices: tuple[DeviceInfo, ...]) -> str:
    headers = ("MAC", "IP", "IDENTITY", "VERSION", "BOARD", "INTERFACE")
    rows = [
        (
            device.mac or "-",
            ",".join(device.ipv4_addresses) or device.source_ip or "-",
            device.identity or "-",
            device.version or "-",
            device.board or "-",
            ",".join(device.interfaces) or "-",
        )
        for device in devices
    ]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    header = "  ".join(value.ljust(widths[i]) for i, value in enumerate(headers))
    divider = "  ".join("-" * width for width in widths)
    body = ["  ".join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in rows]
    return "\n".join((header, divider, *body))


if __name__ == "__main__":
    raise SystemExit(main())
