# STB Buddy Desktop contributor rules

`CLAUDE.md` is a symlink to this file. Edit `AGENTS.md`.

## Purpose

STB Buddy Desktop owns one serial console and shares it with a browser, MCP
clients, REST clients, and a local PTY. `docs/spec.md` is the behavioral source
of truth. Requirement labels in code comments refer to that document.

## Layout

| Path | Role |
|---|---|
| `src/stb_buddy_desktop/__main__.py` | Command-line options and Uvicorn startup. |
| `src/stb_buddy_desktop/desktop.py` | Serial port, history, PTY, TX lock, notes, and transfers. |
| `src/stb_buddy_desktop/server.py` | Thin MCP adapter. |
| `src/stb_buddy_desktop/web.py` | FastAPI, REST, WebSocket, static UI, and mounted MCP app. |
| `src/stb_buddy_desktop/web/static/` | Browser terminal and vendored assets. |
| `tests/fakeboard.py` | Synthetic PTY-backed board used by tests. |
| `tests/smoke_test.py` | Automated end-to-end smoke test. |
| `tests/browser_check.py` | Headless Firefox checks and screenshot capture. |

## Required checks

```bash
uv sync --locked
uv run ruff check .
uv run python tests/smoke_test.py
```

Use the fake board for routine tests. Do not access a physical serial adapter
unless the user explicitly requests a hardware test.

## Design rules

- Update the specification before changing observable behavior.
- Keep serial and synchronization logic in `desktop.py`; keep MCP and HTTP
  adapters thin.
- MCP tools and corresponding REST endpoints must remain equivalent.
- Writes that represent one command must continue to use the transmit lock.
- Browser assets must remain self-contained; do not add CDN dependencies.
- Update `CHANGELOG.md` for user-visible changes.
- Public fixtures, docs, logs, and screenshots must use synthetic data. Never
  commit credentials, private addresses, MAC addresses, device identifiers, or
  real board console output.
- Keep vendored license and notice files with their assets.

## Important implementation details

- The mounted MCP application's lifespan must be entered from FastAPI's
  lifespan; mounting it does not run that lifespan automatically.
- Mount MCP last because its `/` mount matches every remaining path.
- Blocking serial operations belong in synchronous FastAPI/MCP handlers so
  framework worker threads run them.
- Event callbacks execute on serial or writer threads while the history lock
  is held. Schedule WebSocket work with `loop.call_soon_threadsafe`.
- PTY writes must remain non-blocking, and the process must keep its own slave
  descriptor open.
- Use `ToolError` for expected MCP failures whose text must reach the client.
- Use `minicom -o`; otherwise it can send a modem initialization string.
- Restart the fake board together with the service because `TIOCEXCL` remains
  on the PTY while the fake board holds its master descriptor.
