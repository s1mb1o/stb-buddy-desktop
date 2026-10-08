# Contributing

Issues and pull requests are welcome.

## Development setup

```bash
git clone https://github.com/s1mb1o/stb-buddy-desktop.git
cd stb-buddy-desktop
uv sync --locked
uv run ruff check .
uv run python tests/smoke_test.py
```

The smoke test uses a synthetic PTY-backed board and never opens a physical
serial adapter.

## Change guidelines

- Update `docs/spec.md` before changing observable behavior.
- Keep the REST and MCP forms of an operation equivalent.
- Put serial state, locking, and transfer logic in `desktop.py`; keep the HTTP
  and MCP adapters thin.
- Add or extend a fake-board test for behavior changes.
- Do not include real console logs, IP addresses, MAC addresses, credentials,
  device serial numbers, or screenshots from a production STB.
- Keep browser dependencies vendored so the interface works without a CDN.
- Update `CHANGELOG.md` for user-visible changes.

## Hardware testing

Use a disposable development board or read-only commands unless the test
explicitly requires more. Never run flash erase, environment reset, firmware
write, or bootloader modification as an incidental test.
