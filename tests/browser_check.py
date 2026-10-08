"""Drive headless Firefox over Marionette: open the web terminal in light and dark mode,
print the status bar and the last terminal rows, save screenshots. Needs a running desktop service.

Usage: see TESTING.md.
"""
import base64
import json
import socket
import sys
import time

out_dir, port = sys.argv[1], int(sys.argv[2])
base_url = sys.argv[3].rstrip("/") if len(sys.argv) > 3 else "http://127.0.0.1:8765"
test_clear_history = "--clear-history" in sys.argv[4:]


class Marionette:
    def __init__(self, port):
        for _ in range(60):
            try:
                self.s = socket.create_connection(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.5)
        self.buf = b""
        self.id = 0
        self._recv()  # hello

    def _recv(self):
        while b":" not in self.buf:
            self.buf += self.s.recv(65536)
        n, rest = self.buf.split(b":", 1)
        n = int(n)
        while len(rest) < n:
            rest += self.s.recv(1 << 20)
        self.buf = rest[n:]
        return json.loads(rest[:n])

    def cmd(self, name, params=None):
        self.id += 1
        data = json.dumps([0, self.id, name, params or {}]).encode()
        self.s.sendall(str(len(data)).encode() + b":" + data)
        while True:
            msg = self._recv()
            if msg[0] == 1 and msg[1] == self.id:
                if msg[2]:
                    raise RuntimeError(msg[2])
                return msg[3]


m = Marionette(port)
m.cmd("WebDriver:NewSession", {"capabilities": {}})
m.cmd("WebDriver:SetWindowRect", {"width": 1280, "height": 720})


def shot(name):
    png = m.cmd("WebDriver:TakeScreenshot", {"full": False})["value"]
    with open(f"{out_dir}/{name}.png", "wb") as screenshot:
        screenshot.write(base64.b64decode(png))


def js(script):
    return m.cmd("WebDriver:ExecuteScript", {"script": script, "args": []})["value"]


for scheme in ("light", "dark"):
    # 0 = dark, 1 = light (content override for prefers-color-scheme)
    m.cmd("Marionette:SetContext", {"value": "chrome"})
    js(f'Services.prefs.setIntPref("layout.css.prefers-color-scheme.content-override", {0 if scheme == "dark" else 1});')
    m.cmd("Marionette:SetContext", {"value": "content"})
    m.cmd("WebDriver:Navigate", {"url": f"{base_url}/"})
    time.sleep(3)
    info = js("""return {
        scheme: matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
        link: document.getElementById('link').textContent,
        device: document.getElementById('device').textContent,
        clients: document.getElementById('clients').textContent,
        agents: document.getElementById('agents').textContent,
        clearHistory: document.getElementById('clear-history').textContent,
        rows: document.querySelector('.xterm-rows') ? document.querySelector('.xterm-rows').innerText.split('\\n').filter(Boolean).slice(-8) : null,
    };""")
    print(scheme, json.dumps(info, indent=1))
    shot(f"term-{scheme}")
    if scheme == "light" and test_clear_history:
        js("window.confirm = () => true; document.getElementById('clear-history').click(); return true;")
        time.sleep(2)
        cleared = js("""return {
            disabled: document.getElementById('clear-history').disabled,
            rows: document.querySelector('.xterm-rows') ? document.querySelector('.xterm-rows').innerText.split('\\n').filter((line) => line.trim()) : null,
        };""")
        print("clear-history", json.dumps(cleared, indent=1))
        if cleared["disabled"] or cleared["rows"]:
            raise RuntimeError(f"Clear History did not leave an empty, enabled terminal: {cleared}")
        shot("term-light-cleared")
    if scheme == "dark":
        m.cmd("WebDriver:Navigate", {"url": f"{base_url}/docs"})
        time.sleep(2)
        shot("docs-dark")
m.cmd("WebDriver:DeleteSession")
