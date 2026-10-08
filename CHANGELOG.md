# Changelog

All notable user-visible changes are recorded here.

## 1.0.0 — 2026-10-09

- Renamed the project from its internal `serial-hub` name to
  `stb-buddy-desktop` throughout the package, CLI, MCP identity, web UI,
  documentation, and runtime state paths.
- Prepared the first public release with an MIT license, security policy,
  contributor guidance, automated CI smoke test, and sanitized screenshots.
- Changed the default HTTP bind address from `0.0.0.0` to `127.0.0.1`.
  Trusted-LAN access remains available with `--host 0.0.0.0`.
- Changed the portable default device path to `/dev/ttyUSB0`; a stable
  `/dev/serial/by-id/` path remains recommended.
- Includes the shared browser terminal, MCP and REST APIs, PTY bridge,
  immediate reply-on-match, persistent logs, history clearing, and verified
  serial/`nc` file downloads.
