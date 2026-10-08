// Web terminal for STB Buddy Desktop. WebSocket protocol and requirements: docs/spec.md (R16-R22).
"use strict";

const THEMES = {
  light: {
    background: "#fbfbfa", foreground: "#1f2328", cursor: "#1f2328", selectionBackground: "#b6d6fd",
    black: "#24292f", red: "#cf222e", green: "#116329", yellow: "#4d2d00", blue: "#0969da",
    magenta: "#8250df", cyan: "#1b7c83", white: "#6e7781",
    brightBlack: "#57606a", brightRed: "#a40e26", brightGreen: "#1a7f37", brightYellow: "#633c01",
    brightBlue: "#218bff", brightMagenta: "#a475f9", brightCyan: "#3192aa", brightWhite: "#8c959f",
  },
  dark: {
    background: "#0f1115", foreground: "#d7dae0", cursor: "#d7dae0", selectionBackground: "#264f78",
    black: "#484f58", red: "#ff7b72", green: "#3fb950", yellow: "#d29922", blue: "#58a6ff",
    magenta: "#bc8cff", cyan: "#39c5cf", white: "#b1bac4",
    brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#56d364", brightYellow: "#e3b341",
    brightBlue: "#79c0ff", brightMagenta: "#d2a8ff", brightCyan: "#56d4dd", brightWhite: "#f0f6fc",
  },
};

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const theme = () => (darkQuery.matches ? THEMES.dark : THEMES.light);

const term = new Terminal({
  scrollback: 100000, // R18
  cursorBlink: true,
  fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
  fontSize: 13,
  theme: theme(),
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("terminal"));
fit.fit();
term.focus();
new ResizeObserver(() => fit.fit()).observe(document.getElementById("terminal"));
darkQuery.addEventListener("change", () => { term.options.theme = theme(); }); // R17

const encoder = new TextEncoder();
let ws = null;
let atLineStart = true;

function resetHistory() {
  term.reset();
  atLineStart = true;
}

// R19: agent and REST writes as a colored line; palette magenta follows the theme
function writeNote(note) {
  term.write((atLineStart ? "" : "\r\n") + `\x1b[1;35m[${note.source}] ${note.text}\x1b[0m\r\n`);
  atLineStart = true;
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  ws.onopen = resetHistory; // the service replays on connect (R18)
  ws.onmessage = (event) => {
    if (typeof event.data === "string") {
      const message = JSON.parse(event.data);
      if (message.type === "clear") resetHistory();
      else writeNote(message);
      return;
    }
    const bytes = new Uint8Array(event.data);
    if (bytes.length) atLineStart = bytes[bytes.length - 1] === 10;
    term.write(bytes);
  };
  ws.onclose = () => { setTimeout(connect, 2000); }; // R22
}

function send(bytes) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(bytes);
}
term.onData((data) => send(encoder.encode(data)));
term.onBinary((data) => send(Uint8Array.from(data, (c) => c.charCodeAt(0))));

// R20: status bar
const $ = (id) => document.getElementById(id);
let messageTimer = null;

function showMessage(text) {
  $("message").textContent = text;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { $("message").textContent = ""; }, 5000);
}

function render(status) {
  const online = status && status.connected;
  $("link").textContent = status ? (online ? "connected" : "disconnected") : "service offline";
  $("link").className = `pill ${online ? "on" : "off"}`;
  if (!status) return;
  $("device").textContent = `${status.device.split("/").pop()} · ${status.baud}`;
  $("device").title = status.device;
  $("clients").textContent = `${status.web_clients} browser${status.web_clients === 1 ? "" : "s"}`;
  $("agents").textContent = status.agents.length
    ? "agents: " + status.agents.map((a) => `${a.name} (${Math.round(a.idle_s)} s)`).join(", ")
    : "";
}

async function poll() {
  try {
    const response = await fetch("/api/status");
    render(response.ok ? await response.json() : null);
  } catch {
    render(null);
  } finally {
    setTimeout(poll, 2000);
  }
}

// R21
$("break").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/break", { method: "POST" });
    if (!response.ok) showMessage((await response.json()).detail || `BREAK failed: ${response.status}`);
  } catch {
    showMessage("BREAK failed: service offline");
  }
  term.focus();
});

// R45-R49: remove the current server history and clear every connected web terminal.
$("clear-history").addEventListener("click", async () => {
  if (!window.confirm("Clear all console history for this session? This cannot be undone.")) {
    term.focus();
    return;
  }
  $("clear-history").disabled = true;
  try {
    const response = await fetch("/api/clear_history", { method: "POST" });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      showMessage(errorText(body, response.status));
    } else {
      resetHistory(); // also works if the WebSocket is temporarily disconnected
    }
  } catch {
    showMessage("Clear History failed: service offline");
  } finally {
    $("clear-history").disabled = false;
    term.focus();
  }
});

// R38: The service performs the shell check, transfer selection, download, and hash verification.
const dialog = $("dl-dialog");
let downloading = false;
let finished = false; // after a successful download, the primary button is Close

function setStatus(kind, ...children) {
  $("dl-status").className = `dl-status ${kind}`;
  $("dl-status").replaceChildren(...children);
}

function setBusy(on) {
  downloading = on;
  for (const id of ["dl-path", "dl-start", "dl-cancel"]) $(id).disabled = on;
}

function setFinished(on) {
  finished = on;
  $("dl-start").textContent = on ? "Close" : "Download";
  $("dl-cancel").hidden = on;
}

function resultList(result) {
  const list = document.createElement("dl");
  list.className = "dl-result";
  const ips = result.board_ips === null ? "not checked (console only)" : result.board_ips.join(", ") || "none found";
  const rows = [
    ["File", result.name],
    ["Size", `${result.size.toLocaleString()} bytes`],
    ["Transport", `${result.transport} · ${result.seconds} s · ${result.retries} retries`],
    ["Board IP", ips],
    ["SHA-256 board", result.sha256_source, "mono"],
    ["SHA-256 copy", result.sha256_local, "mono"],
  ];
  for (const [label, value, cls] of rows) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    if (cls) dd.className = cls;
    list.append(dt, dd);
  }
  return list;
}

function saveFile(name) {
  const link = document.createElement("a");
  link.href = `/api/downloads/${encodeURIComponent(name)}`;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
}

function errorText(body, status) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return detail || `HTTP ${status}`;
}

$("download").addEventListener("click", () => {
  if (!downloading) {
    setStatus("");
    setFinished(false);
  }
  dialog.showModal();
  $("dl-path").select();
});
$("dl-cancel").addEventListener("click", () => dialog.close());
$("dl-path").addEventListener("input", () => { if (finished) setFinished(false); }); // a new path: Download again
// Esc while busy: block the key itself, because a `cancel` event is not always cancelable (close
// watcher rules). Listen on document: the disabled field loses focus, so keys don't pass the dialog.
document.addEventListener("keydown", (event) => { if (downloading && event.key === "Escape") event.preventDefault(); }, true);
dialog.addEventListener("cancel", (event) => { if (downloading) event.preventDefault(); });
dialog.addEventListener("close", () => term.focus());

$("dl-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (finished) {
    dialog.close();
    return;
  }
  const path = $("dl-path").value.trim();
  if (!path || downloading) return;
  setBusy(true);
  const started = Date.now();
  const tick = () => setStatus("busy", `Working… ${Math.round((Date.now() - started) / 1000)} s. The terminal shows each command.`);
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      setStatus("error", errorText(body, response.status));
      setBusy(false);
      $("dl-path").focus(); // try again
      return;
    }
    const match = document.createElement("p");
    match.className = "ok";
    match.textContent = body.sha256_source === body.sha256_local ? "✓ SHA-256 of the board file and the copy match. The browser saves the file." : "";
    setStatus("done", match, resultList(body));
    saveFile(body.name);
    setBusy(false);
    setFinished(true);
    $("dl-start").focus();
  } catch {
    setStatus("error", "Download failed: service offline");
  } finally {
    clearInterval(timer);
    setBusy(false);
  }
});

connect();
poll();
