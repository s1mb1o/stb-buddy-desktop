"""Fake STB console on a PTY: echoes input, answers a few commands, prints '/ # '.

Usage: python3 fakeboard.py <link-path>   (the desktop service opens <link-path> as its --device)

Commands that start with `wc `, `dd ` or `cat `, and `echo` with a `$`, run in sh on this host,
with tests/bin (a BusyBox-style `uuencode`) first on PATH, so that `download` works (spec
R26-R29, R36). `ifconfig` prints BusyBox-style output with eth0 at 10.0.0.2 (R37).
`noise N` adds a synthetic kernel message to the output of each such command: in the
middle of a line for every Nth command, between two lines for the others. `noise 0` turns it off.
`reboot` prints a boot log and a synthetic initramfs menu `Press [01R] to change rootfs source:`
with a countdown, and waits 3 s for one key, without Enter (spec R39-R44). It prints the key and
the seconds from the menu to the key.
"""
import os
import select
import signal
import subprocess
import sys
import time
import tty

link = sys.argv[1]
master, slave = os.openpty()
tty.setraw(slave)
if os.path.islink(link):
    os.unlink(link)
os.symlink(os.ttyname(slave), link)
PROMPT = b"/ # "
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")
ENV = dict(os.environ, PATH=BIN + os.pathsep + os.environ["PATH"])
IFCONFIG = (
    b"eth0      Link encap:Ethernet  HWaddr 02:00:00:00:00:01  \r\n"
    b"          inet addr:10.0.0.2  Bcast:0.0.0.0  Mask:255.255.255.0\r\n"
    b"          UP BROADCAST RUNNING MULTICAST  MTU:1500  Metric:1\r\n\r\n"
    b"lo        Link encap:Local Loopback  \r\n"
    b"          inet addr:127.0.0.1  Mask:255.0.0.0\r\n"
    b"          UP LOOPBACK RUNNING  MTU:16436  Metric:1\r\n\r\n"
)
KERNEL = b"[123.456000] fake-storage: corrected one synthetic read error\r\n"
noise = 0
shell_runs = 0


def out(data: bytes) -> None:
    os.write(master, data)


def boot_menu() -> None:
    """Like src/initramfs/common/init: one key per `read -s -n1 -t1`, counter 2, 1, 0."""
    out(b"Restarting system.\r\n")
    time.sleep(0.5)
    out(b"\x1b[0mU-Boot 2020.01 (fake)\r\nStarting kernel ...\r\n" + b"=" * 50 + b"\r\n")
    out(b"0. Early login\r\n1. Early login with network\r\nR. Reboot\r\nPress [01R] to change rootfs source:    ")
    shown = time.monotonic()
    for counter in (2, 1, 0):
        out(b"\b\b%d " % counter)
        ready, _, _ = select.select([master], [], [], 1.0)
        if ready:
            key = os.read(master, 1)  # the rest, e.g. a CR, stays for the shell
            out(b"\r\nkey %r after %.3f s\r\n" % (key, time.monotonic() - shown))
            return
    out(b"\r\nno key, booting from flash\r\n\x1b[32mWelcome to fakebox\x1b[0m\r\n")


def run_shell(cmd: str) -> None:
    """Run cmd in sh. Ctrl-C on the console kills it. The output goes out when cmd ends."""
    global shell_runs
    proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV, start_new_session=True)
    output = b""
    while True:
        ready, _, _ = select.select([proc.stdout, master], [], [])
        if master in ready and b"\x03" in os.read(master, 1024):
            os.killpg(proc.pid, signal.SIGKILL)  # the whole pipeline, like Ctrl-C on a real tty
            proc.wait()
            out(b"^C\r\n")
            return
        if proc.stdout in ready:
            data = os.read(proc.stdout.fileno(), 65536)
            if not data:
                break
            output += data
    proc.wait()
    output = output.replace(b"\n", b"\r\n")
    shell_runs += 1
    if noise and len(output) > 200:
        mid = len(output) // 2
        if shell_runs % noise:
            mid = output.index(b"\n", mid) + 1  # between two lines
        output = output[:mid] + KERNEL + output[mid:]
    out(output)


out(b"\x1b[0mU-Boot 2020.01 (fake)\r\nStarting kernel ...\r\n\x1b[32mWelcome to fakebox\x1b[0m\r\n" + PROMPT)
line = b""
while True:
    try:
        data = os.read(master, 1024)
    except OSError:
        time.sleep(0.1)
        continue
    for b in data:
        c = bytes([b])
        if c == b"\r" or c == b"\n":
            out(b"\r\n")
            cmd = line.decode(errors="replace").strip()
            line = b""
            if cmd == "uname -a":
                out(b"Linux fakebox 5.4.0 #1 SMP armv7l GNU/Linux\r\n")
            elif cmd.startswith("echo ") and "$" not in cmd:
                out(cmd[5:].encode() + b"\r\n")
            elif cmd == "ifconfig":
                out(IFCONFIG)
            elif cmd.startswith(("wc ", "dd ", "cat ", "echo ")):
                run_shell(cmd)
            elif cmd.startswith("noise "):
                noise = int(cmd[6:])
            elif cmd.startswith("sleep "):
                time.sleep(float(cmd[6:]))
            elif cmd == "reboot":
                boot_menu()
            elif cmd == "big":
                for i in range(3000):
                    out(b"line %06d the quick brown fox jumps over the lazy dog\r\n" % i)
            elif cmd:
                out(f"-sh: {cmd}: not found\r\n".encode())
            out(PROMPT)
        elif c == b"\x03":
            out(b"^C\r\n" + PROMPT)
            line = b""
        else:
            line += c
            out(c)
