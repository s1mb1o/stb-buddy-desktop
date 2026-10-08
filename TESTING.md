# Testing

Use the synthetic fake board for routine development. It creates a PTY and
implements a small shell-like console without touching physical hardware.

## Automated smoke test

```bash
uv sync --locked
uv run ruff check .
uv run python tests/smoke_test.py
```

The smoke test starts the fake board and STB Buddy Desktop on an isolated
loopback port. It verifies:

- serial connection and status;
- command send/wait and output cleanup;
- REST and OpenAPI identity;
- MCP server identity, version, and seven-tool catalog;
- a SHA-256-verified serial file download;
- clear-history offset behavior;
- clean service shutdown.

## Manual fake-board session

```bash
test_dir=$(mktemp -d)
PATH="$PWD/tests/bin:$PATH" python3 tests/fakeboard.py "$test_dir/fakedev" &
uv run stb-buddy-desktop \
  --device "$test_dir/fakedev" \
  --pty-link "$test_dir/ttySTB" \
  --log-dir "$test_dir/logs" \
  --download-dir "$test_dir/downloads"
```

Open <http://127.0.0.1:8765/> and run safe synthetic commands such as
`uname -a`, `echo hello`, `sleep 1`, and `reboot`.

Restart the fake board whenever you restart the desktop service. The service
sets `TIOCEXCL` on the PTY, and that flag persists while the fake board keeps
the PTY master open.

## Browser check and screenshots

`tests/browser_check.py` drives Firefox through Marionette. Use it only with
the fake board when creating public screenshots.

1. Create a disposable Firefox profile outside the repository.
2. Start Firefox headless with Marionette enabled.
3. Start the fake board and service as above.
4. Add a few synthetic commands through `/api/send_and_wait`.
5. Run:

```bash
python3 tests/browser_check.py <profile-dir> <marionette-port> http://127.0.0.1:8765
```

The script captures light and dark terminal images and a dark API image. Read
the terminal rows printed by the script and inspect every image before adding
one to the repository.

## Optional hardware checks

Hardware tests are manual and must use an explicitly supplied
`/dev/serial/by-id/` path.

- Verify the correct voltage, ground, RX/TX crossover, baud rate, and pinout.
- Start with `status`, `read`, and a known read-only shell command.
- Confirm that a second process cannot open the raw adapter.
- Confirm that unplug/replug changes `connected` to false and back to true.
- Exercise file downloads only from a non-sensitive test file.
- Inspect screenshots and logs for identifiers or credentials before sharing.

Do not use destructive bootloader, flash, or environment commands merely to
test the transport.
