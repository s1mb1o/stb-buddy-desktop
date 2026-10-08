"""MCP tools. A thin layer: tool docs for agents, calls into StbBuddyDesktop.

Sync tools are fine here: MCPServer (mcp 2.x) runs them on a worker thread, so blocking
waits in StbBuddyDesktop don't stall the event loop.
"""

from __future__ import annotations

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .desktop import (
    Chunk,
    ClearHistoryResult,
    CommandResult,
    DownloadResult,
    Status,
    StbBuddyDesktop,
    Transport,
    WaitResult,
    WriteResult,
)

INSTRUCTIONS = """\
This server owns one serial console (an STB board). Other agents and humans (web terminal,
minicom) may use the same console at the same time, and all of them see the same output.

- Run shell commands with send_and_wait; it holds a lock so commands don't interleave.
- Output is addressed by absolute byte offsets. Keep the `next` value from each result
  and pass it as `since` to read or wait_for, so you don't miss or repeat output.
- Send control characters as JSON escapes in write, e.g. "\\u0003" for Ctrl-C.
- You react too slowly for boot prompts that wait a few seconds. Pass `reply` to wait_for or
  send_and_wait, and the desktop service types it the moment the pattern matches.
- Humans see every write as a note line tagged with your client name.
- Copy a file from the board with download. It checks that the SHA-256 of both sides match.
- clear_history irreversibly removes the retained console output and notes for this process session.
  It sends nothing to the board and does not remove older session logs or downloaded files.
"""


def _source(ctx: Context) -> str:
    params = ctx.session.client_params
    return params.client_info.name if params and params.client_info else "mcp"


def build_server(desktop: StbBuddyDesktop) -> MCPServer:
    mcp = MCPServer("stb-buddy-desktop", instructions=INSTRUCTIONS)

    @mcp.tool()
    def status() -> Status:
        """Port, connection state, session, log and PTY paths, buffer end, active clients.

        Use `buffer_end` as the first `since` value.
        """
        return desktop.status()

    @mcp.tool()
    def read(ctx: Context, since: int | None = None, max_bytes: int = 65536, timeout: float = 0.0) -> Chunk:
        """Read console output from offset `since`. The read doesn't consume data.

        since=None returns the last `max_bytes`. With timeout > 0, wait up to that many
        seconds for output after `since`.
        """
        return desktop.read(since, max_bytes, timeout, _source(ctx))

    @mcp.tool()
    def write(ctx: Context, data: str, newline: bool = True) -> WriteResult:
        """Send raw text to the console; newline=True appends Enter (CR). Doesn't wait for a reply.

        `offset` is where the reply starts; pass it as `since` to read or wait_for.
        """
        return desktop.write(data.encode() + (b"\r" if newline else b""), _source(ctx))

    @mcp.tool()
    def wait_for(
        ctx: Context, pattern: str, since: int | None = None, timeout: float = 30.0, reply: str | None = None
    ) -> WaitResult:
        """Wait until console output after `since` matches the Python regex `pattern`.

        since=None means "from now". Use it for boot output, e.g. "Hit any key" or "login:".
        With `reply`, the desktop service sends `reply` the moment `pattern` matches:
        use it for prompts that wait only a few seconds, e.g. reply="1" for "Press \\[01R\\]".
        `reply` is sent as is, without Enter; add "\\r" if the prompt needs it. A match on old
        output after `since` also sends the reply, so pass since=None or a fresh `next`.
        """
        return desktop.wait_for(pattern, since, timeout, _source(ctx), reply)

    @mcp.tool()
    def send_and_wait(
        ctx: Context, command: str, pattern: str | None = None, timeout: float = 30.0, reply: str | None = None
    ) -> CommandResult:
        """Run a shell command: send `command` + Enter, wait for `pattern` (default: the shell prompt).

        Returns the command output without the echoed command line and the prompt line.
        With `reply`, the desktop service sends `reply` without Enter the moment `pattern` matches,
        e.g. command="reboot", pattern="Press \\[01R\\]", reply="1" to stop at a boot menu.
        """
        return desktop.send_and_wait(command, pattern or desktop.config.prompt, timeout, _source(ctx), reply)

    @mcp.tool()
    def download(
        ctx: Context, path: str, name: str | None = None, transport: Transport = "auto", timeout: float = 600.0
    ) -> DownloadResult:
        """Copy the file `path` from the board to the desktop host. Fails if its SHA-256 differs.

        The board must be at the shell prompt; the service checks it with `echo` first. transport="auto"
        uses base64 chunks over the console (about 8 KB/s) for small files, and tries nc over the
        LAN first for files above --nc-min-size (64 KiB by default). `timeout` is for the whole
        call, 600 s max. `name` (default: the last component of `path`) is the file name on the desktop host.
        """
        try:
            return desktop.download(path, name, transport, timeout, _source(ctx))
        except (RuntimeError, ValueError) as e:  # mcp 2.x shows the model the text of a ToolError only
            raise ToolError(str(e)) from e

    @mcp.tool()
    def clear_history(ctx: Context) -> ClearHistoryResult:
        """Irreversibly clear retained console output and notes; send nothing to the board.

        Open web terminals are cleared too. Offsets stay monotonic; use `next` as the next `since`.
        Older session logs and downloaded files are not removed.
        """
        return desktop.clear_history(_source(ctx))

    return mcp
