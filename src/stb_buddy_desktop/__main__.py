"""Entry point for `stb-buddy-desktop`. Option defaults are listed in docs/spec.md."""

from __future__ import annotations

import argparse
import logging
import socket
from pathlib import Path

import uvicorn

from .desktop import DesktopConfig, StbBuddyDesktop
from .server import build_server
from .web import build_app

DEFAULT_DEVICE = "/dev/ttyUSB0"


def default_ip() -> str:
    """IPv4 address of the interface with the default route (R28). A UDP connect sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="stb-buddy-desktop",
        description="Share one STB serial console with browsers, MCP clients, scripts, and terminal programs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--device", default=DEFAULT_DEVICE, help="serial device path")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (no auth: 0.0.0.0 exposes the console to the LAN)",
    )
    p.add_argument("--http-port", type=int, default=8765, help="HTTP port for /, /api, /docs, /mcp")
    p.add_argument("--pty-link", type=Path, default=Path("/tmp/ttySTB"), help="symlink to the PTY for minicom")
    p.add_argument("--log-dir", type=Path, default=Path("~/.local/state/stb-buddy-desktop").expanduser())
    p.add_argument("--prompt", default=r"[#$] $", help="default send_and_wait regex")
    p.add_argument(
        "--download-dir", type=Path, default=Path("~/.local/state/stb-buddy-desktop/downloads").expanduser()
    )
    p.add_argument("--nc-host", default=default_ip(), help="address that the board connects to for nc downloads")
    p.add_argument("--nc-min-size", type=int, default=65536, help="download transport auto uses serial up to this size")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    desktop = StbBuddyDesktop(
        DesktopConfig(
            device=args.device,
            baud=args.baud,
            pty_link=args.pty_link,
            log_dir=args.log_dir,
            prompt=args.prompt,
            download_dir=args.download_dir,
            nc_host=args.nc_host,
            nc_min_size=args.nc_min_size,
        )
    )
    app = build_app(desktop, build_server(desktop), args.host)
    uvicorn.run(app, host=args.host, port=args.http_port, log_level="info")


if __name__ == "__main__":
    main()
