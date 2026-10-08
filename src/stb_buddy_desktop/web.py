"""HTTP app: web terminal (/ and /ws), REST API (/api, OpenAPI at /docs), MCP mounted at /mcp.

Requirement numbers (R11...) refer to docs/spec.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from . import __version__
from .desktop import (
    Chunk,
    ClearHistoryResult,
    CommandResult,
    DownloadResult,
    Event,
    Note,
    Status,
    StbBuddyDesktop,
    Transport,
    WaitResult,
    WriteResult,
)

STATIC = Path(__file__).parent / "web" / "static"
REPLAY_LINES = 100_000  # R18
CHUNK = 64 * 1024

DESCRIPTION = """\
REST access to one shared serial console. The same operations exist as MCP tools at `/mcp`.

Output is addressed by absolute byte offsets in the session log. Keep `next` from each
result and pass it as `since` on the next call. The web terminal is at `/`.
"""


class WriteRequest(BaseModel):
    data: str = Field(description='Text to send. Control characters as JSON escapes, e.g. "\\u0003" for Ctrl-C.')
    newline: bool = Field(True, description="Append Enter (CR).")


class WaitRequest(BaseModel):
    pattern: str = Field(description="Python regex.", examples=["login:"])
    since: int | None = Field(None, ge=0, description='Offset to scan from. Null means "from now".')
    timeout: float = Field(30.0, ge=0, le=600)
    reply: str | None = Field(
        None, description="Text to send at once when `pattern` matches. Sent as is, without Enter.", examples=["1"]
    )


class CommandRequest(BaseModel):
    command: str = Field(examples=["uname -a"])
    pattern: str | None = Field(None, description="Python regex that ends the output. Null means the shell prompt.")
    timeout: float = Field(30.0, ge=0, le=600)
    reply: str | None = Field(
        None, description="Text to send at once when `pattern` matches. Sent as is, without Enter.", examples=["1"]
    )


class BreakRequest(BaseModel):
    duration: float = Field(0.25, gt=0, le=5, description="BREAK length in seconds.")


class DownloadRequest(BaseModel):
    path: str = Field(description="File on the board.", examples=["/etc/hosts"])
    name: str | None = Field(None, description="File name on the desktop host. Null means the last component of `path`.")
    transport: Transport = Field(
        "auto", description="auto: serial for small files; above --nc-min-size, nc over the LAN first, then serial."
    )
    timeout: float = Field(600.0, gt=0, le=600, description="Limit for the whole download.")


class Ok(BaseModel):
    ok: bool = True


def _source(request: Request) -> str:
    return f"api@{request.client.host}" if request.client else "api"


def _note_json(note: Note) -> str:
    return json.dumps({"type": "note", "source": note.source, "text": note.text, "offset": note.offset})


def _clear_json(next_: int) -> str:
    return json.dumps({"type": "clear", "next": next_})


def build_app(desktop: StbBuddyDesktop, mcp: MCPServer, host: str) -> FastAPI:
    mcp_app = mcp.streamable_http_app(host=host)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        desktop.start()
        try:
            # A mounted app's lifespan doesn't run by itself; MCP needs it for its session manager.
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            desktop.stop()

    app = FastAPI(
        title="STB Buddy Desktop",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url=None,  # served below with vendored assets (R16, R24)
        redoc_url=None,
    )

    @app.exception_handler(RuntimeError)
    async def busy_or_disconnected(request: Request, exc: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(re.error)
    async def bad_pattern(request: Request, exc: re.error) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": f"invalid regex: {exc}"})

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # --- REST API (R24). Sync handlers run on FastAPI's thread pool, so blocking waits are fine. ---

    @app.get("/api/status", tags=["console"])
    def api_status() -> Status:
        """Port, connection state, session, log and PTY paths, buffer end, active clients."""
        return desktop.status()

    @app.get("/api/read", tags=["console"])
    def api_read(
        request: Request,
        since: int | None = Query(None, ge=0, description="Offset to read from. Omit for the last `max_bytes`."),
        max_bytes: int = Query(65536, gt=0, le=4 * 1024 * 1024),
        timeout: float = Query(0.0, ge=0, le=600, description="Seconds to wait for output after `since`."),
    ) -> Chunk:
        """Read console output. The read doesn't consume data."""
        return desktop.read(since, max_bytes, timeout, _source(request))

    @app.post("/api/write", tags=["console"])
    def api_write(request: Request, body: WriteRequest) -> WriteResult:
        """Send raw text. Doesn't wait for a reply; read from `offset` to see it."""
        return desktop.write(body.data.encode() + (b"\r" if body.newline else b""), _source(request))

    @app.post("/api/wait_for", tags=["console"])
    def api_wait_for(request: Request, body: WaitRequest) -> WaitResult:
        """Wait until output after `since` matches `pattern`. With `reply`, send it the moment `pattern` matches."""
        return desktop.wait_for(body.pattern, body.since, body.timeout, _source(request), body.reply)

    @app.post("/api/send_and_wait", tags=["console"])
    def api_send_and_wait(request: Request, body: CommandRequest) -> CommandResult:
        """Run a shell command and return its output. Holds the console until `pattern` or timeout.

        With `reply`, send it the moment `pattern` matches.
        """
        return desktop.send_and_wait(
            body.command, body.pattern or desktop.config.prompt, body.timeout, _source(request), body.reply
        )

    @app.post("/api/break", tags=["console"])
    def api_break(request: Request, body: BreakRequest | None = None) -> Ok:
        """Send a serial BREAK."""
        desktop.send_break((body or BreakRequest()).duration, _source(request))
        return Ok()

    @app.post("/api/clear_history", tags=["console"])
    def api_clear_history(request: Request) -> ClearHistoryResult:
        """Irreversibly clear the retained console history and notes; send nothing to the board."""
        return desktop.clear_history(_source(request))

    @app.get("/api/log", tags=["console"], response_class=StreamingResponse)
    def api_log() -> StreamingResponse:
        """Download the session log (raw bytes) up to now."""
        fd, size = desktop.log_snapshot()  # fixed inode and size: append/clear cannot change this download (R47)

        def chunks() -> Iterator[bytes]:
            try:
                for pos in range(0, size, CHUNK):
                    yield os.pread(fd, min(CHUNK, size - pos), pos)
            finally:
                os.close(fd)

        headers = {
            "Content-Disposition": f'attachment; filename="{desktop.session}.log"',
            "Content-Length": str(size),
        }
        return StreamingResponse(chunks(), media_type="text/plain; charset=utf-8", headers=headers)

    @app.post("/api/download", tags=["download"])
    def api_download(request: Request, body: DownloadRequest) -> DownloadResult:
        """Copy a file from the board to the desktop host. Fails if the SHA-256 differs (R25-R35)."""
        return desktop.download(body.path, body.name, body.transport, body.timeout, _source(request))

    @app.get("/api/downloads/{name}", tags=["download"], response_class=FileResponse)
    def api_downloaded(name: str) -> FileResponse:
        """Fetch a finished download (R34)."""
        file = desktop.downloaded(name)
        if file is None:
            raise HTTPException(status_code=404, detail=f"no download named {name!r}")
        return FileResponse(file, media_type="application/octet-stream", filename=name)

    # --- web terminal (R16-R22) ---

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        page = get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title="STB Buddy Desktop API",
            swagger_js_url="/static/vendor/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/vendor/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/vendor/swagger-ui/favicon-32x32.png",
        )
        extra = '<meta name="color-scheme" content="light dark"><link rel="stylesheet" href="/static/swagger-dark.css">'
        return HTMLResponse(page.body.decode().replace("</head>", extra + "</head>", 1))

    @app.websocket("/ws")
    async def terminal(ws: WebSocket) -> None:
        await ws.accept()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Event] = asyncio.Queue()

        def on_event(event: Event) -> None:  # runs on the RX or writer thread
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # loop closed during shut-down
                pass

        end, notes = desktop.subscribe(on_event)
        sender: asyncio.Task[None] | None = None
        try:
            start = await anyio.to_thread.run_sync(desktop.line_start, REPLAY_LINES, end)
            pos = start
            for note in notes:  # replay with notes in order (R18)
                if note.offset >= start:
                    pos = await _send_raw(ws, desktop, pos, note.offset)
                    await ws.send_text(_note_json(note))
            await _send_raw(ws, desktop, pos, end)
            sender = asyncio.create_task(_pump(ws, queue))
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes") or (message.get("text") or "").encode()
                if data:
                    await anyio.to_thread.run_sync(desktop.write_human, data)
        except WebSocketDisconnect:
            pass
        finally:
            desktop.unsubscribe(on_event)
            if sender:
                sender.cancel()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/", mcp_app)  # last, so it only gets what no route above matched: /mcp (R23)
    return app


async def _send_raw(ws: WebSocket, desktop: StbBuddyDesktop, pos: int, stop: int) -> int:
    while pos < stop:
        data = desktop.read_raw(pos, min(stop, pos + CHUNK))
        await ws.send_bytes(data)
        pos += len(data)
    return pos


async def _pump(ws: WebSocket, queue: asyncio.Queue[Event]) -> None:
    try:
        while True:
            event = await queue.get()
            if event[0] == "rx":
                await ws.send_bytes(event[2])
            elif event[0] == "note":
                await ws.send_text(_note_json(event[1]))
            else:
                await ws.send_text(_clear_json(event[1]))
    except (WebSocketDisconnect, RuntimeError):  # closed while sending
        pass
