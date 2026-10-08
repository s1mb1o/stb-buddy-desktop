"""End-to-end smoke test using the synthetic PTY-backed board."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(base_url: str, path: str, body: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content = response.read()
        return json.loads(content) if content else None


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-4000:]


def main() -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="stb-buddy-desktop-test-") as directory:
        temp = Path(directory)
        device = temp / "fake-device"
        source = temp / "synthetic.txt"
        source.write_bytes(b"STB Buddy Desktop synthetic download fixture\n")
        fake_log = temp / "fake-board.log"
        service_log = temp / "service.log"
        env = os.environ.copy()
        env["PATH"] = f"{ROOT / 'tests' / 'bin'}{os.pathsep}{env['PATH']}"

        with fake_log.open("wb") as fake_output, service_log.open("wb") as service_output:
            fake = subprocess.Popen(
                [sys.executable, str(ROOT / "tests" / "fakeboard.py"), str(device)],
                cwd=ROOT,
                env=env,
                stdout=fake_output,
                stderr=subprocess.STDOUT,
            )
            service: subprocess.Popen[bytes] | None = None
            try:
                for _ in range(100):
                    if device.exists():
                        break
                    if fake.poll() is not None:
                        raise RuntimeError("fake board exited before creating its device")
                    time.sleep(0.05)
                else:
                    raise RuntimeError("fake board did not create its device")

                service = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "stb_buddy_desktop",
                        "--device",
                        str(device),
                        "--host",
                        "127.0.0.1",
                        "--http-port",
                        str(port),
                        "--pty-link",
                        str(temp / "ttySTB"),
                        "--log-dir",
                        str(temp / "logs"),
                        "--download-dir",
                        str(temp / "downloads"),
                    ],
                    cwd=ROOT,
                    stdout=service_output,
                    stderr=subprocess.STDOUT,
                )

                status = None
                for _ in range(200):
                    if service.poll() is not None:
                        raise RuntimeError("desktop service exited during startup")
                    try:
                        status = request(base_url, "/api/status", timeout=1)
                        if status["connected"]:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("desktop service did not connect to the fake board")

                assert status is not None
                assert status["device"] == str(device)
                assert status["baud"] == 115200

                command = request(base_url, "/api/send_and_wait", {"command": "uname -a"})
                assert command["matched"] is True
                assert command["output"] == "Linux fakebox 5.4.0 #1 SMP armv7l GNU/Linux"

                openapi = request(base_url, "/openapi.json")
                assert openapi["info"]["title"] == "STB Buddy Desktop"
                assert "/api/download" in openapi["paths"]

                result = request(
                    base_url,
                    "/api/download",
                    {"path": str(source), "name": "synthetic.txt", "transport": "serial", "timeout": 30},
                    timeout=40,
                )
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                downloaded = temp / "downloads" / "synthetic.txt"
                assert result["sha256_source"] == digest
                assert result["sha256_local"] == digest
                assert result["transport"] == "serial"
                assert downloaded.read_bytes() == source.read_bytes()

                before_clear = request(base_url, "/api/status")
                cleared = request(base_url, "/api/clear_history", {})
                after_clear = request(base_url, "/api/status")
                assert cleared["cleared_bytes"] > 0
                assert cleared["next"] == before_clear["buffer_end"]
                assert after_clear["buffer_start"] == cleared["next"]
                assert after_clear["buffer_end"] == cleared["next"]
            except (AssertionError, KeyError, OSError, RuntimeError, ValueError, urllib.error.URLError):
                if service is not None:
                    stop(service)
                stop(fake)
                raise RuntimeError(
                    "smoke test failed\n\nservice log:\n"
                    + log_tail(service_log)
                    + "\n\nfake-board log:\n"
                    + log_tail(fake_log)
                ) from None
            else:
                if service is not None:
                    stop(service)
                stop(fake)

    print("STB Buddy Desktop smoke test passed")


if __name__ == "__main__":
    main()
