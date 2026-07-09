# ============================================================
#  SLAVE AGENT  (run one of these on every drone-laptop)
#  ---------------------------------------------------------
#  Lets the master dashboard remotely:
#    • start / stop / restart the drone controller
#    • read live status (drone link, battery, flying, running)
#    • SCAN nearby Wi-Fi and CONNECT the drone to a chosen SSID
#    • report the public Tailscale Funnel player link
#
#  Run:   python agent.py
#  Then point the master dashboard at this laptop's LAN IP.
# ============================================================
import os, re, sys, json, time, socket, threading, subprocess
from collections import deque
from flask import Flask, request, jsonify

# ---- EDIT THESE PER LAPTOP ---------------------------------
AGENT_NAME      = os.environ.get("AGENT_NAME", socket.gethostname())
AGENT_PORT      = 9000
TOKEN           = os.environ.get("AGENT_TOKEN", "airschool123456")  # MUST match "token" in the master's fleet.json
DRONE_IFACE     = "Wi-Fi"                   # the drone-facing Wi-Fi adapter (netsh wlan show interfaces)
APP_FILE        = "controller_manual_a.py"  # MUST match the real filename sitting next to this agent
CONTROLLER_PORT = 8080
# APP_DIR defaults to this script's own folder — copy the folder anywhere, no edit needed.
APP_DIR         = os.environ.get("APP_DIR", os.path.dirname(os.path.abspath(__file__)))
# ------------------------------------------------------------

app = Flask(__name__)

def app_path():
    return os.path.join(APP_DIR, APP_FILE)

# ─────────────────────────────────────────
#  Which Python runs the controller?
#  pyhula usually lives in a venv, NOT in the system Python. Prefer a venv sitting
#  next to the controller; otherwise fall back to whatever runs this agent.
#  Override explicitly with:  set PYTHON_EXE=C:\path\to\.venv\Scripts\python.exe
# ─────────────────────────────────────────
def _in_venv():
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)

def _detect_python():
    override = os.environ.get("PYTHON_EXE")
    if override:
        return override
    if _in_venv():
        return sys.executable        # agent was already launched from a venv — use it
    # Otherwise look for ANY venv folder next to the controller (.venv, venv, .venv12, ...)
    try:
        for entry in sorted(os.listdir(APP_DIR)):
            for sub in (os.path.join("Scripts", "python.exe"), os.path.join("bin", "python")):
                cand = os.path.join(APP_DIR, entry, sub)
                if os.path.isfile(cand):
                    return cand
    except OSError:
        pass
    return sys.executable

PYTHON_EXE = _detect_python()

def check_pyhula():
    """Confirm the interpreter we'll launch the controller with can import pyhula."""
    try:
        r = subprocess.run([PYTHON_EXE, "-c", "import pyhula"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, ""
        tail = (r.stderr or "").strip().splitlines()
        return False, (tail[-1] if tail else "import pyhula failed")
    except Exception as ex:
        return False, str(ex)

_pyhula_ok, _pyhula_err = True, ""   # filled in at startup

# ─────────────────────────────────────────
#  Managed controller subprocess (crash-auto-restart)
# ─────────────────────────────────────────
_proc = None
_want_running = False
_started_at = 0.0
_last_error = ""
_fail_count = 0
_next_try = 0.0
_log = deque(maxlen=200)
_proc_lock = threading.Lock()

def _spawn():
    """Launch the controller. Returns True on success; records why on failure."""
    global _proc, _started_at, _last_error
    path = app_path()
    if not os.path.isfile(path):
        others = [f for f in os.listdir(APP_DIR) if f.startswith("controller") and f.endswith(".py")] \
                 if os.path.isdir(APP_DIR) else []
        _last_error = f"controller not found: {path}" + (f" | did you mean: {', '.join(others)}" if others else "")
        _log.append("[agent] ERROR " + _last_error)
        return False
    try:
        _log.append(f"[agent] launching {APP_FILE} with {PYTHON_EXE} ...")
        # A piped child defaults to the system codepage (cp874 on Thai Windows), so any
        # emoji print would crash it. Force UTF-8 + unbuffered so logs stream live.
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["CONTROL_TOKEN"] = TOKEN     # authenticates our /land calls to the controller
        _proc = subprocess.Popen(
            [PYTHON_EXE, APP_FILE],
            cwd=APP_DIR, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
    except Exception as ex:
        _last_error = f"failed to launch {path}: {ex}"
        _log.append("[agent] ERROR " + _last_error)
        return False

    _started_at = time.time()
    _last_error = ""
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    return True

def _reader(p):
    try:
        for line in p.stdout:
            _log.append(line.rstrip())
    except Exception:
        pass

def _is_running():
    return _proc is not None and _proc.poll() is None

def _supervisor():
    """Keeps the controller alive while the master wants it running.
    Never dies on error, and backs off if the controller keeps crashing."""
    global _fail_count, _next_try, _last_error
    while True:
        time.sleep(1)
        try:
            with _proc_lock:
                if _is_running():
                    if time.time() - _started_at > 10:
                        _fail_count = 0          # survived long enough → consider it healthy
                    continue
                if not _want_running:
                    continue

                # Wanted but not running. Did it die almost immediately?
                if _proc is not None and _started_at and (time.time() - _started_at) < 5:
                    _fail_count += 1
                    _last_error = "controller exited immediately — see /logs"

                if time.time() < _next_try:
                    continue
                if not _spawn():
                    _fail_count += 1

                if _fail_count:
                    _next_try = time.time() + min(30, 2 ** min(_fail_count, 5))
                    if _fail_count == 3:
                        _log.append("[agent] controller keeps failing - check the lines above")
        except Exception as ex:
            _log.append(f"[agent] supervisor error: {ex}")

def ctrl_start():
    global _want_running, _fail_count, _next_try
    with _proc_lock:
        _want_running = True
        _fail_count, _next_try = 0, 0.0          # clear any backoff on an explicit Start
        if _is_running():
            return True
        return _spawn()

def ctrl_stop():
    global _want_running
    with _proc_lock:
        _want_running = False
        if _is_running():
            _log.append("[agent] stopping controller")
            try:
                _proc.terminate()
            except Exception:
                pass

def ctrl_restart():
    ctrl_stop()
    time.sleep(1.5)
    ctrl_start()

threading.Thread(target=_supervisor, daemon=True).start()

# ─────────────────────────────────────────
#  Helpers: LAN IP, controller health, funnel link, Wi-Fi
# ─────────────────────────────────────────
def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))          # picks the internet-facing (LAN) adapter
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def controller_health():
    """Ask the controller's /health for drone link / battery / flying."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CONTROLLER_PORT}/health", timeout=1.2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None

def funnel_link():
    """Parse `tailscale funnel status` for the public https URL."""
    try:
        out = subprocess.run(["tailscale", "funnel", "status"],
                             capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"https://[^\s]+", out)
        if m:
            return m.group(0).rstrip("/")
    except Exception:
        pass
    return None

def _wlan_interfaces():
    """Parse `netsh wlan show interfaces` into one dict per adapter."""
    out = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                         capture_output=True, text=True, timeout=6).stdout
    blocks, cur = [], None
    for line in out.splitlines():
        s = line.strip()
        if ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip().upper(), v.strip()
        if k == "NAME":                 # each adapter's block starts with Name
            cur = {"name": v}
            blocks.append(cur)
        elif cur is not None:
            if k == "SSID":             # exact match: never catches BSSID
                cur["ssid"] = v
            elif k == "STATE":
                cur["state"] = v
    return blocks

def wlan_iface_names():
    try:
        return [b.get("name") for b in _wlan_interfaces()]
    except Exception:
        return []

def current_ssid(iface=None):
    """SSID of the DRONE adapter specifically.

    A laptop here has two Wi-Fi adapters (drone + internet). The old code returned the
    first SSID line in the whole output, which was the internet one.
    """
    want = (iface or DRONE_IFACE).lower()
    try:
        blocks = _wlan_interfaces()
    except Exception:
        return None
    for b in blocks:
        if b.get("name", "").lower() == want:
            return b.get("ssid")        # None when that adapter is disconnected
    # DRONE_IFACE doesn't match any adapter. Report nothing rather than the wrong
    # adapter's SSID — a misconfigured DRONE_IFACE should be visible, not disguised.
    return None

def wifi_scan():
    """List nearby SSIDs (from the last scan) so the master can offer a dropdown."""
    try:
        out = subprocess.run(["netsh", "wlan", "show", "networks", f"interface={DRONE_IFACE}"],
                             capture_output=True, text=True, timeout=8).stdout
    except Exception:
        out = ""
    ssids, seen = [], set()
    for line in out.splitlines():
        m = re.match(r"\s*SSID\s+\d+\s*:\s*(.+?)\s*$", line)
        if m:
            name = m.group(1).strip()
            if name and name not in seen:
                seen.add(name)
                ssids.append(name)
    return ssids

def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))

def connect_wifi(ssid, password=None):
    """(Re)create a Wi-Fi profile for `ssid` and connect the drone adapter to it."""
    e_ssid = _xml_escape(ssid)
    if password:
        sec = (f"<authentication>WPA2PSK</authentication><encryption>AES</encryption>"
               f"<useOneX>false</useOneX></authEncryption>"
               f"<sharedKey><keyType>passPhrase</keyType><protected>false</protected>"
               f"<keyMaterial>{_xml_escape(password)}</keyMaterial></sharedKey>")
    else:
        sec = "<authentication>open</authentication><encryption>none</encryption><useOneX>false</useOneX></authEncryption>"

    xml = (f'<?xml version="1.0"?>'
           f'<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">'
           f'<name>{e_ssid}</name>'
           f'<SSIDConfig><SSID><name>{e_ssid}</name></SSID></SSIDConfig>'
           f'<connectionType>ESS</connectionType><connectionMode>manual</connectionMode>'
           f'<MSM><security><authEncryption>{sec}</security></MSM></WLANProfile>')

    path = os.path.join(os.environ.get("TEMP", "."), "drone_wifi_profile.xml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)

    logs = []
    for cmd in (
        ["netsh", "wlan", "add", "profile", f"filename={path}", f"interface={DRONE_IFACE}"],
        ["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}", f"interface={DRONE_IFACE}"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            logs.append((r.stdout + r.stderr).strip())
        except Exception as ex:
            logs.append(str(ex))
    return "\n".join(logs)

# ─────────────────────────────────────────
#  HTTP API (token-protected, LAN-only)
# ─────────────────────────────────────────
def _auth_ok():
    tok = request.headers.get("X-Token") or request.args.get("token")
    return tok == TOKEN

@app.before_request
def _guard():
    if request.path == "/ping":
        return  # unauthenticated liveness check
    if not _auth_ok():
        return jsonify(ok=False, error="unauthorized"), 401

@app.route("/ping")
def ping():
    return jsonify(ok=True, name=AGENT_NAME)

@app.route("/status")
def status():
    h = controller_health() or {}
    return jsonify(
        ok=True,
        name=AGENT_NAME,
        lan_ip=get_lan_ip(),
        running=_is_running(),
        uptime=int(time.time() - _started_at) if _is_running() else 0,
        drone_connected=h.get("connected"),
        battery=h.get("battery", "--"),
        flying=h.get("flying"),
        camera=h.get("camera"),
        ssid=current_ssid(),
        drone_iface=DRONE_IFACE,
        ifaces=wlan_iface_names(),      # lets the master spot a wrong DRONE_IFACE
        link=funnel_link(),
        app=app_path(),                       # so the master can spot a bad path instantly
        app_ok=os.path.isfile(app_path()),
        python=PYTHON_EXE,
        pyhula_ok=_pyhula_ok,
        pyhula_err=_pyhula_err,
        agent_error=_last_error,
    )

@app.route("/start",   methods=["POST"])
def _start():
    ok = ctrl_start()
    return jsonify(ok=bool(ok), error=_last_error, app=app_path())

@app.route("/stop",    methods=["POST"])
def _stop():    ctrl_stop();    return jsonify(ok=True)

@app.route("/restart", methods=["POST"])
def _restart():
    ctrl_restart()
    return jsonify(ok=_is_running() or not _last_error, error=_last_error)

@app.route("/land", methods=["POST"])
def _land():
    """Emergency land — forwarded to the controller's authenticated /land route."""
    import urllib.request, urllib.error
    req = urllib.request.Request(f"http://127.0.0.1:{CONTROLLER_PORT}/land",
                                 method="POST", headers={"X-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return jsonify(json.loads(r.read().decode()))
    except urllib.error.HTTPError as ex:
        try:
            return jsonify(json.loads(ex.read().decode()))
        except Exception:
            return jsonify(ok=False, error=f"HTTP {ex.code}")
    except Exception as ex:
        return jsonify(ok=False, error=f"controller unreachable: {ex}")

@app.route("/wifi_scan")
def _scan():
    return jsonify(ok=True, ssids=wifi_scan(), current=current_ssid())

@app.route("/connect_wifi", methods=["POST"])
def _connect():
    data = request.get_json(force=True, silent=True) or {}
    ssid = (data.get("ssid") or "").strip()
    if not ssid:
        return jsonify(ok=False, error="missing ssid"), 400
    out = connect_wifi(ssid, data.get("password") or None)
    return jsonify(ok=True, ssid=ssid, output=out)

@app.route("/logs")
def _logs():
    return jsonify(ok=True, lines=list(_log))

if __name__ == "__main__":
    print(f"[AGENT] '{AGENT_NAME}' on http://{get_lan_ip()}:{AGENT_PORT}  (token required)")
    print(f"[AGENT] Put this IP in the master's fleet.json -> \"host\": \"{get_lan_ip()}\"")
    print(f"[AGENT] Controller: {app_path()}")
    if os.path.isfile(app_path()):
        print("[AGENT] Controller file found. OK.")
    else:
        cands = [f for f in os.listdir(APP_DIR) if f.startswith("controller") and f.endswith(".py")]
        print("[AGENT] *** ERROR: controller file NOT FOUND. START will do nothing. ***")
        print(f"[AGENT] *** Set APP_FILE to one of: {cands or 'no controller_*.py in this folder'}")

    print(f"[AGENT] Python for controller: {PYTHON_EXE}")
    print("[AGENT] Checking 'import pyhula' with that interpreter...")
    _pyhula_ok, _pyhula_err = check_pyhula()
    if _pyhula_ok:
        print("[AGENT] pyhula import OK.")
    else:
        print("[AGENT] *** ERROR: this Python CANNOT import pyhula. START will fail. ***")
        print(f"[AGENT] *** {_pyhula_err}")
        print(r"[AGENT] *** Fix: run the agent with your venv, e.g. .venv\Scripts\python.exe agent.py")
        print(r"[AGENT] ***  or: set PYTHON_EXE=C:\path\to\.venv\Scripts\python.exe")

    print(f"[AGENT] Drone Wi-Fi adapter: '{DRONE_IFACE}'   Token set: {'yes' if TOKEN else 'NO'}")
    app.run(host="0.0.0.0", port=AGENT_PORT, threaded=True)
