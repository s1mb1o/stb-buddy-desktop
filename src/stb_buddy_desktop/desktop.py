"""Serial port owner: RX reader, session log as the RX buffer, PTY mirror, TX lock.

Requirement numbers (R1...) refer to docs/spec.md.
"""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import logging
import os
import re
import select
import shlex
import socket
import termios
import threading
import time
import tty
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import serial
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

ENTER = b"\r"  # R3a
TX_LOCK_TIMEOUT = 60.0  # R12
AGENT_WINDOW = 600.0  # R15
REOPEN_INTERVAL = 1.0  # R3
CHUNK_SIZE = 16384  # R29
CHUNK_ATTEMPTS = 4  # R29: the first try and 3 more
NC_CONNECT_WAIT = 5.0  # R28
DOWNLOAD_TIMEOUT = 600.0  # R32
READY_COMMAND = "echo READY_$((40+2))"  # R36: only a shell prints READY_42
READY_TIMEOUT = 5.0  # R36

Transport = Literal["auto", "nc", "serial"]

_SIZE_LINE = re.compile(r"^\s*(\d+)\s*$", re.MULTILINE)  # wc -c
_SHA_LINE = re.compile(r"^([0-9a-f]{64})\s", re.MULTILINE)  # sha256sum: "<hash>  <file or ->"
_B64_LINE = re.compile(r"[A-Za-z0-9+/]+=*")
_READY_LINE = re.compile(r"^READY_42$", re.MULTILINE)
_INET = re.compile(r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+)")  # ifconfig, BusyBox and net-tools formats

# OSC (ESC ] ... BEL/ST), CSI (ESC [ ... final), then two-byte escapes
_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def clean(data: bytes) -> str:
    """Raw console bytes -> text for MCP and REST results (R13)."""
    text = _ANSI.sub("", data.decode("utf-8", errors="replace"))
    return text.replace("\r\n", "\n").replace("\r", "")


def _decode_chunk(text: str) -> bytes | None:
    """Output of the R29 chunk command -> chunk data, or None if the data is damaged."""
    sha = _SHA_LINE.search(text)
    lines = text.split("\n")
    try:
        first = next(i for i, line in enumerate(lines) if line.startswith("begin-base64 ")) + 1
        last = lines.index("====", first)
    except (StopIteration, ValueError):
        return None
    encoded = "".join(line for line in lines[first:last] if _B64_LINE.fullmatch(line))  # drops kernel messages
    try:
        data = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return None
    return data if sha and hashlib.sha256(data).hexdigest() == sha[1] else None


def _valid_name(name: str) -> bool:
    """R25: a plain file name in the download directory."""
    return bool(name) and "/" not in name and "\0" not in name and name not in (".", "..") and not name.endswith(".part")


def caret(text: str) -> str:
    """Control characters in caret notation, for note lines (R14)."""
    return "".join("^?" if c == "\x7f" else f"^{chr(ord(c) + 64)}" if c < " " and c != "\t" else c for c in text)


class AgentInfo(BaseModel):
    name: str
    idle_s: float = Field(description="Seconds since the last call from this source.")


class Status(BaseModel):
    device: str
    baud: int
    connected: bool
    session: str = Field(description="Session name. Offsets restart from 0 in a new session.")
    log_file: str
    buffer_start: int = Field(description="Oldest retained offset. It advances when history is cleared.")
    buffer_end: int = Field(description="Offset after the last received byte. Use it as the first `since`.")
    pty_link: str
    pty_dropped: int = Field(description="Bytes not delivered to the PTY because nothing was reading it.")
    web_clients: int
    agents: list[AgentInfo] = Field(description="Sources that called a tool or API endpoint in the last 10 minutes.")


class Chunk(BaseModel):
    text: str
    start: int = Field(description="Offset of the first byte in `text`.")
    next: int = Field(description="Pass as `since` on the next call.")


class WriteResult(BaseModel):
    written: int
    offset: int = Field(description="Where the reply starts. Pass as `since` to read or wait_for.")


class WaitResult(BaseModel):
    matched: bool
    match: str | None
    text: str = Field(description="All output scanned from `since`.")
    next: int = Field(description="End of the scanned output. Pass as `since` on the next call.")


class CommandResult(BaseModel):
    output: str = Field(description="Command output without the echoed command line and the prompt line.")
    matched: bool = Field(description="False if `pattern` did not appear before the timeout.")
    next: int = Field(description="End of the scanned output. Pass as `since` on the next call.")


class DownloadResult(BaseModel):
    file: str = Field(description="Path of the file on the desktop host.")
    name: str = Field(description="File name. Other hosts fetch the file with GET /api/downloads/{name}.")
    size: int
    sha256_source: str = Field(description="SHA-256 of the file on the board.")
    sha256_local: str = Field(description="SHA-256 of the file on the desktop host. Always equal to sha256_source.")
    transport: Literal["nc", "serial"] = Field(description="The transport that delivered the data.")
    seconds: float
    retries: int = Field(description="Chunk commands that were sent again because the chunk was damaged.")
    board_ips: list[str] | None = Field(
        description="IPv4 addresses of the board from ifconfig, for display. Null when the service did not try nc."
    )


class ClearHistoryResult(BaseModel):
    cleared_bytes: int
    cleared_notes: int
    next: int = Field(description="First offset after the cleared history. New output starts here.")


@dataclass(frozen=True)
class Note:
    offset: int
    source: str
    text: str
    time: float


# ("rx", offset, data), ("note", Note), or ("clear", next); delivered on the RX or caller thread
Event = tuple
Subscriber = Callable[[Event], None]


@dataclass(frozen=True)
class DesktopConfig:
    device: str
    baud: int
    pty_link: Path
    log_dir: Path
    prompt: str
    download_dir: Path
    nc_host: str
    nc_min_size: int


class StbBuddyDesktop:
    def __init__(self, config: DesktopConfig) -> None:
        self.config = config
        self.session = "console-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.log_file = config.log_dir.expanduser() / f"{self.session}.log"
        self._buf_lock = threading.Lock()  # guards the log fds, _start, _end, _notes, _subscribers
        self._rx_ready = threading.Condition(self._buf_lock)  # notified on new RX bytes
        self._tx_lock = threading.Lock()  # serializes agent and REST writes (R12)
        self._port_lock = threading.Lock()  # guards _port for writers and close
        self._port: serial.Serial | None = None
        self._start = 0
        self._end = 0
        self._notes: list[Note] = []
        self._subscribers: list[Subscriber] = []
        self._agents: dict[str, float] = {}
        self._pty_dropped = 0
        self._running = False
        self._threads: list[threading.Thread] = []

    # --- lifecycle ---

    def start(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_wr = os.open(self.log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self._log_rd = os.open(self.log_file, os.O_RDONLY)
        self._pty_master, self._pty_slave = os.openpty()  # keep the slave open (R10)
        tty.setraw(self._pty_slave)
        os.set_blocking(self._pty_master, False)  # R9
        self._pty_name = os.ttyname(self._pty_slave)
        link = self.config.pty_link
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise RuntimeError(f"{link} exists and is not a symlink")  # R7
        link.symlink_to(self._pty_name)
        self._running = True
        for target in (self._rx_loop, self._pty_loop):
            thread = threading.Thread(target=target, name=target.__name__, daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("session log %s; PTY %s -> %s", self.log_file, link, self._pty_name)

    def stop(self) -> None:
        self._running = False
        for thread in self._threads:
            thread.join(timeout=2)
        self._close_port()
        link = self.config.pty_link
        if link.is_symlink() and os.readlink(link) == self._pty_name:
            link.unlink()
        for fd in (self._pty_master, self._pty_slave, self._log_wr, self._log_rd):
            os.close(fd)

    # --- serial port and RX ---

    def _open_port(self) -> serial.Serial:
        port = serial.Serial(self.config.device, self.config.baud, timeout=0.1, exclusive=True)
        try:
            fcntl.ioctl(port.fileno(), termios.TIOCEXCL)  # R1
        except OSError:
            port.close()
            raise
        return port

    def _close_port(self) -> None:
        with self._port_lock:
            port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except (serial.SerialException, OSError):
                pass

    def _rx_loop(self) -> None:
        last_error = ""
        while self._running:
            port = self._port
            if port is None:
                try:
                    port = self._open_port()
                except (serial.SerialException, OSError) as e:
                    if str(e) != last_error:
                        log.warning("cannot open %s: %s (retrying every %.0f s)", self.config.device, e, REOPEN_INTERVAL)
                        last_error = str(e)
                    time.sleep(REOPEN_INTERVAL)  # R3
                    continue
                with self._port_lock:
                    self._port = port
                last_error = ""
                log.info("opened %s at %d baud", self.config.device, self.config.baud)
                continue
            try:
                data = port.read(port.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                log.warning("lost %s: %s", self.config.device, e)
                self._close_port()
                continue
            if data:
                self._append(data)

    def _append(self, data: bytes) -> None:
        with self._buf_lock:
            os.write(self._log_wr, data)  # R4: on disk before the offset moves, so readers always find it
            offset = self._end
            self._end += len(data)
            self._rx_ready.notify_all()
            self._emit(("rx", offset, data))
        try:
            written = os.write(self._pty_master, data)
        except OSError:  # BlockingIOError when nobody reads the PTY (R9)
            written = 0
        self._pty_dropped += len(data) - written

    def _emit(self, event: Event) -> None:
        # Called with _buf_lock held, so events reach subscribers in offset order.
        for subscriber in self._subscribers:
            subscriber(event)

    def _pty_loop(self) -> None:
        while self._running:
            ready, _, _ = select.select([self._pty_master], [], [], 0.5)
            if not ready:
                continue
            try:
                data = os.read(self._pty_master, 4096)
            except BlockingIOError:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            self.write_human(data)

    # --- TX ---

    def _port_write(self, data: bytes) -> None:
        with self._port_lock:
            if self._port is None:
                raise RuntimeError(f"serial port {self.config.device} is not connected")
            self._port.write(data)

    @contextmanager
    def _tx(self) -> Iterator[None]:
        if not self._tx_lock.acquire(timeout=TX_LOCK_TIMEOUT):
            raise RuntimeError("console busy: another client has held the console for 60 s")
        try:
            yield
        finally:
            self._tx_lock.release()

    def _send(self, data: bytes, source: str, label: str | None = None) -> int:
        """Record a note and write. Call with the TX lock held. Returns the offset before the write."""
        if self._port is None:
            raise RuntimeError(f"serial port {self.config.device} is not connected")
        text = label or caret(data.decode("utf-8", errors="replace").removesuffix(ENTER.decode()))
        with self._buf_lock:
            offset = self._end
            note = Note(offset, source, text, time.time())
            self._notes.append(note)
            self._emit(("note", note))
        self._port_write(data)
        return offset

    def write_human(self, data: bytes) -> None:
        """Input from the browser or the PTY: no TX lock, no note, dropped while disconnected (R12)."""
        try:
            self._port_write(data)
        except (RuntimeError, serial.SerialException, OSError):
            pass

    def write(self, data: bytes, source: str) -> WriteResult:
        self._touch(source)
        with self._tx():
            return WriteResult(written=len(data), offset=self._send(data, source))

    def send_break(self, duration: float, source: str) -> None:
        self._touch(source)
        with self._tx():
            self._send(b"", source, label="<BREAK>")
            with self._port_lock:
                if self._port is None:
                    raise RuntimeError(f"serial port {self.config.device} is not connected")
                self._port.send_break(duration)

    # --- reading ---

    def span(self) -> tuple[int, int]:
        with self._buf_lock:
            return self._start, self._end

    def end(self) -> int:
        with self._buf_lock:
            return self._end

    def read_raw(self, start: int, end: int) -> bytes:
        with self._buf_lock:
            start = min(max(start, self._start), self._end)
            end = min(max(end, start), self._end)
            return os.pread(self._log_rd, end - start, start - self._start) if end > start else b""

    def log_snapshot(self) -> tuple[int, int]:
        """Duplicate the current log fd and return (fd, size), so a clear cannot change the download (R47)."""
        with self._buf_lock:
            return os.dup(self._log_rd), self._end - self._start

    def _wait_data(self, since: int, timeout: float) -> int:
        """Wait until there are bytes after `since` or the timeout ends. Returns the end offset."""
        deadline = time.monotonic() + timeout
        with self._rx_ready:
            while self._end <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._rx_ready.wait(remaining)
            return self._end

    def _scan(self, since: int, pattern: re.Pattern[str], timeout: float) -> tuple[re.Match[str] | None, str, int]:
        deadline = time.monotonic() + timeout
        end = self.end()
        while True:
            text = clean(self.read_raw(since, end))
            match = pattern.search(text)
            remaining = deadline - time.monotonic()
            if match or remaining <= 0:
                return match, text, end
            end = self._wait_data(end, remaining)

    def read(self, since: int | None, max_bytes: int, timeout: float, source: str) -> Chunk:
        self._touch(source)
        start, end = self.span()
        since = max(start, end - max_bytes) if since is None else min(max(since, start), end)  # R6, R46
        if timeout > 0:
            self._wait_data(since, timeout)
            start, end = self.span()
            since = min(max(since, start), end)
        stop = min(end, since + max_bytes)
        return Chunk(text=clean(self.read_raw(since, stop)), start=since, next=stop)

    def wait_for(self, pattern: str, since: int | None, timeout: float, source: str, reply: str | None = None) -> WaitResult:
        self._touch(source)
        regex = re.compile(pattern)
        start, end = self.span()
        since = end if since is None else min(max(since, start), end)
        with self._tx() if reply else nullcontext():  # R41: no other agent can delay the reply
            match, text, next_ = self._scan(since, regex, timeout)
            if match and reply:
                self._send(reply.encode(), source)  # R39, R40: at once, no Enter
        return WaitResult(matched=match is not None, match=match.group(0) if match else None, text=text, next=next_)

    def send_and_wait(
        self, command: str, pattern: str, timeout: float, source: str, reply: str | None = None
    ) -> CommandResult:
        self._touch(source)
        regex = re.compile(pattern)
        with self._tx():  # held until the prompt shows, so commands don't interleave (R12)
            since = self._send(command.encode() + ENTER, source)
            match, text, next_ = self._scan(since, regex, timeout)
            if match and reply:
                self._send(reply.encode(), source)  # R39, R40: at once, no Enter
        if match:
            text = text[: text.rfind("\n", 0, match.start()) + 1]  # cut the whole prompt line
        lines = text.split("\n")
        if lines[0].strip() == command.strip():  # the echoed command line
            lines = lines[1:]
        return CommandResult(output="\n".join(lines).removesuffix("\n"), matched=match is not None, next=next_)

    # --- file download (R25-R38) ---

    def download(self, path: str, name: str | None, transport: Transport, timeout: float, source: str) -> DownloadResult:
        """Copy a file from the board to the download directory. The SHA-256 of both sides must match."""
        self._touch(source)
        name = name or PurePosixPath(path).name
        if not _valid_name(name):
            raise ValueError(f"invalid file name {name!r}: pass a plain `name`")
        if transport not in ("auto", "nc", "serial"):
            raise ValueError(f"invalid transport {transport!r}")
        started = time.monotonic()
        deadline = started + min(timeout, DOWNLOAD_TIMEOUT)  # R32
        quoted = shlex.quote(path)
        directory = self.config.download_dir.expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        dest, part = directory / name, directory / f"{name}.part"
        try:
            self._check_shell(deadline, source)
            pre = self.send_and_wait(f"wc -c < {quoted}; sha256sum {quoted}", self.config.prompt, self._left(deadline), source)
            size_match, sha_match = _SIZE_LINE.search(pre.output), _SHA_LINE.search(pre.output)
            if not (pre.matched and size_match and sha_match):  # R26
                raise RuntimeError(f"cannot read {path} on the board: {pre.output.strip() or 'no output'}")
            size, sha_source = int(size_match[1]), sha_match[1]
            used, retries, board_ips = None, 0, None
            if transport == "nc" or (transport == "auto" and size > self.config.nc_min_size):  # R27
                board_ips = self._board_ips(deadline, source)
                if self._nc_transfer(quoted, size, part, deadline, source):
                    used = "nc"
                elif transport == "nc":
                    raise RuntimeError(f"the board did not connect to {self.config.nc_host} in {NC_CONNECT_WAIT:.0f} s")
            if used is None:
                used, retries = "serial", self._serial_transfer(quoted, size, part, deadline, source)
        except TimeoutError as e:
            raise RuntimeError(f"download timed out after {min(timeout, DOWNLOAD_TIMEOUT):.0f} s; partial data in {part}") from e
        local_size = part.stat().st_size
        with part.open("rb") as f:
            sha_local = hashlib.file_digest(f, "sha256").hexdigest()
        if (local_size, sha_local) != (size, sha_source):  # R31
            raise RuntimeError(
                f"hash check failed, kept {part}: source {size} bytes sha256 {sha_source}, "
                f"local {local_size} bytes sha256 {sha_local}"
            )
        part.replace(dest)
        seconds = round(time.monotonic() - started, 1)
        log.info("downloaded %s to %s: %d bytes over %s in %.1f s, %d retries", path, dest, size, used, seconds, retries)
        return DownloadResult(
            file=str(dest),
            name=name,
            size=size,
            sha256_source=sha_source,
            sha256_local=sha_local,
            transport=used,
            seconds=seconds,
            retries=retries,
            board_ips=board_ips,
        )

    def _check_shell(self, deadline: float, source: str) -> None:
        """R36: fail unless the console is at a shell prompt. Sends nothing else to recover."""
        result = self.send_and_wait(READY_COMMAND, self.config.prompt, min(READY_TIMEOUT, self._left(deadline)), source)
        if not (result.matched and _READY_LINE.search(result.output)):
            got = result.output.strip()[-500:] or "no output"
            raise RuntimeError(f"the console is not at a shell prompt (expected READY_42 within {READY_TIMEOUT:.0f} s): {got}")

    def _board_ips(self, deadline: float, source: str) -> list[str]:
        """R37: IPv4 addresses from ifconfig on the board. For display only."""
        result = self.send_and_wait("ifconfig", self.config.prompt, self._left(deadline), source)
        return [ip for ip in dict.fromkeys(_INET.findall(result.output)) if ip != "127.0.0.1"]

    def _nc_transfer(self, quoted: str, size: int, part: Path, deadline: float, source: str) -> bool:
        """R28: the board sends the file with `cat | nc` to an ephemeral port. False if it does not connect."""
        with socket.create_server(("", 0)) as server, self._tx():  # R30: hold the lock until the prompt
            server.settimeout(min(NC_CONNECT_WAIT, self._left(deadline)))
            port = server.getsockname()[1]
            since = self._send(f"cat {quoted} | nc {self.config.nc_host} {port}".encode() + ENTER, source)
            try:
                conn, _ = server.accept()
            except TimeoutError:
                self._interrupt(source)
                return False
            try:
                with conn, part.open("wb") as f:
                    received = 0
                    while received < size:  # stop at `size`: BusyBox nc may not close on EOF (spec open question 7)
                        conn.settimeout(self._left(deadline))
                        data = conn.recv(min(1 << 16, size - received))
                        if not data:
                            break
                        f.write(data)
                        received += len(data)
                if not self._scan(since, re.compile(self.config.prompt), self._left(deadline))[0]:
                    raise TimeoutError
            except TimeoutError:
                self._interrupt(source)  # R32
                raise
        return True

    def _serial_transfer(self, quoted: str, size: int, part: Path, deadline: float, source: str) -> int:
        """R29: base64 chunks over the console, each checked with its SHA-256. Returns the number of retries."""
        retries = 0
        with part.open("wb") as f:
            for n in range(-(-size // CHUNK_SIZE)):
                # 2>&- closes stderr without assuming that the board user can write /dev/null (spec R29)
                dd = f"dd if={quoted} bs={CHUNK_SIZE} skip={n} count=1 2>&-"
                command = f"{dd} | sha256sum; {dd} | uuencode -m x"
                for _ in range(CHUNK_ATTEMPTS):
                    # send_and_wait takes the TX lock for this chunk only (R30)
                    result = self.send_and_wait(command, self.config.prompt, self._left(deadline), source)
                    if not result.matched:
                        raise TimeoutError
                    data = _decode_chunk(result.output)
                    if data is not None:
                        f.write(data)
                        break
                    retries += 1
                    log.warning("chunk %d of %s is damaged, sending it again", n, quoted)
                else:
                    raise RuntimeError(f"chunk {n} of {quoted} was damaged {CHUNK_ATTEMPTS} times")
        return retries

    def _interrupt(self, source: str) -> None:
        """Send Ctrl-C and wait for the prompt. Call with the TX lock held."""
        since = self._send(b"\x03", source)
        self._scan(since, re.compile(self.config.prompt), NC_CONNECT_WAIT)

    @staticmethod
    def _left(deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def downloaded(self, name: str) -> Path | None:
        """A finished download, for GET /api/downloads/{name} (R34)."""
        file = self.config.download_dir.expanduser() / name
        return file if _valid_name(name) and file.is_file() else None

    # --- history (R45-R49) ---

    def clear_history(self, source: str) -> ClearHistoryResult:
        """Atomically replace the current log with an empty one, preserving monotonic logical offsets."""
        self._touch(source)
        temporary = self.log_file.with_name(f".{self.log_file.name}.clear-{os.getpid()}-{time.time_ns()}")
        new_wr = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644)
        try:
            new_rd = os.open(temporary, os.O_RDONLY)
        except BaseException:
            os.close(new_wr)
            temporary.unlink(missing_ok=True)
            raise
        installed = False
        try:
            with self._buf_lock:
                os.replace(temporary, self.log_file)
                installed = True
                old_wr, old_rd = self._log_wr, self._log_rd
                self._log_wr, self._log_rd = new_wr, new_rd
                cleared_bytes = self._end - self._start
                cleared_notes = len(self._notes)
                self._start = self._end
                self._notes.clear()
                self._rx_ready.notify_all()
                self._emit(("clear", self._start))
                os.close(old_wr)
                os.close(old_rd)
            return ClearHistoryResult(cleared_bytes=cleared_bytes, cleared_notes=cleared_notes, next=self._start)
        finally:
            if not installed:
                os.close(new_wr)
                os.close(new_rd)
                temporary.unlink(missing_ok=True)

    # --- web terminal and status ---

    def subscribe(self, subscriber: Subscriber) -> tuple[int, list[Note]]:
        """Register for live events. Returns the end offset and the notes so far, both at registration."""
        with self._buf_lock:
            self._subscribers.append(subscriber)
            return self._end, list(self._notes)

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._buf_lock:
            self._subscribers.remove(subscriber)

    def line_start(self, lines: int, end: int) -> int:
        """Offset where the last `lines` lines before `end` begin (R18)."""
        start, current_end = self.span()
        pos, count = min(max(end, start), current_end), 0
        while pos > start:
            start = self.span()[0]
            if pos <= start:
                return start
            size = min(1 << 20, pos - start)
            pos -= size
            chunk = self.read_raw(pos, pos + size)
            idx = len(chunk)
            while (idx := chunk.rfind(b"\n", 0, idx)) >= 0:
                count += 1
                if count > lines:
                    return pos + idx + 1
        return start

    def _touch(self, source: str) -> None:
        self._agents[source] = time.monotonic()

    def status(self) -> Status:
        now = time.monotonic()
        agents = [
            AgentInfo(name=name, idle_s=round(now - seen, 1))
            for name, seen in sorted(self._agents.items(), key=lambda item: -item[1])
            if now - seen <= AGENT_WINDOW
        ]
        with self._buf_lock:
            start, end, web_clients = self._start, self._end, len(self._subscribers)
        return Status(
            device=self.config.device,
            baud=self.config.baud,
            connected=self._port is not None,
            session=self.session,
            log_file=str(self.log_file),
            buffer_start=start,
            buffer_end=end,
            pty_link=str(self.config.pty_link),
            pty_dropped=self._pty_dropped,
            web_clients=web_clients,
            agents=agents,
        )
