# ============================================================
#  MASTER DASHBOARD  (run this on YOUR laptop)
#  ---------------------------------------------------------
#  One page to run the whole fleet:
#    • live status per drone-laptop (running / drone link / battery / flying)
#    • Start / Stop / Restart each controller
#    • SCAN + CHOOSE the drone Wi-Fi for a slave, then Connect
#    • copy / open each public player link
#
#  Config:  edit fleet.json (same folder)
#  Run:     python master_dashboard.py   → open http://localhost:5000
# ============================================================
import os, json, urllib.request
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, render_template_string

HERE        = os.path.dirname(os.path.abspath(__file__))
FLEET_FILE  = os.path.join(HERE, "fleet.json")
AGENT_PORT  = 9000
DASH_PORT   = 5050   # NOT 5000 — Windows reserves 4905-5004 (Hyper-V/WSL), causing WinError 10013

def load_fleet():
    with open(FLEET_FILE, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("token", ""), cfg.get("slaves", [])

def host_for(name):
    _, slaves = load_fleet()
    for s in slaves:
        if s["name"] == name:
            return s["host"]
    return None

def agent_call(host, path, method="GET", data=None, timeout=6):
    """Call a slave agent server-side (holds the token; browser never sees it)."""
    token, _ = load_fleet()
    url = f"http://{host}:{AGENT_PORT}{path}"
    headers = {"X-Token": token}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

app = Flask(__name__)

# ─────────────────────────────────────────
#  API (browser talks only to here)
# ─────────────────────────────────────────
@app.route("/api/fleet")
def api_fleet():
    _, slaves = load_fleet()

    def probe(s):
        st = agent_call(s["host"], "/status", timeout=4)
        st["name"] = s["name"]
        st["host"] = s["host"]
        if not st.get("ok"):
            st["online"] = False
        else:
            st["online"] = True
        # fall back to a configured link if the agent couldn't detect one
        if not st.get("link") and s.get("funnel_url"):
            st["link"] = s["funnel_url"]
        return st

    with ThreadPoolExecutor(max_workers=8) as ex:
        return jsonify(list(ex.map(probe, slaves)))

@app.route("/api/<name>/<action>", methods=["POST"])
def api_action(name, action):
    if action not in ("start", "stop", "restart", "land"):
        return jsonify(ok=False, error="bad action"), 400
    host = host_for(name)
    if not host:
        return jsonify(ok=False, error="unknown slave"), 404
    return jsonify(agent_call(host, "/" + action, method="POST", timeout=12))

@app.route("/api/land_all", methods=["POST"])
def api_land_all():
    """Emergency: land every drone at once, in parallel (not one after another)."""
    _, slaves = load_fleet()

    def land(s):
        r = agent_call(s["host"], "/land", method="POST", timeout=12)
        return {"name": s["name"], "ok": bool(r.get("ok")), "error": r.get("error", "")}

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(land, slaves))
    return jsonify(ok=all(r["ok"] for r in results), results=results)

@app.route("/api/<name>/logs")
def api_logs(name):
    host = host_for(name)
    if not host:
        return jsonify(ok=False, error="unknown slave"), 404
    return jsonify(agent_call(host, "/logs", timeout=6))

@app.route("/api/<name>/wifi_scan")
def api_scan(name):
    host = host_for(name)
    if not host:
        return jsonify(ok=False, error="unknown slave"), 404
    return jsonify(agent_call(host, "/wifi_scan", timeout=12))

@app.route("/api/<name>/connect_wifi", methods=["POST"])
def api_connect(name):
    host = host_for(name)
    if not host:
        return jsonify(ok=False, error="unknown slave"), 404
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(agent_call(host, "/connect_wifi", method="POST", data=data, timeout=25))

@app.route("/")
def index():
    return render_template_string(DASH_HTML)

# ─────────────────────────────────────────
#  Dashboard page
# ─────────────────────────────────────────
DASH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DRONE FLEET // CONTROL</title>
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<style>
  :root { --accent:#00e5ff; --danger:#ff2d55; --ok:#00ff88; --warn:#f39c12; --panel:rgba(12,20,30,.85); }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:#05080d; color:#dfefff; font-family:'Share Tech Mono',monospace; min-height:100vh; padding:22px; }
  h1 { font-family:'Orbitron',sans-serif; font-weight:900; font-size:1.4rem; color:var(--accent);
       text-shadow:0 0 12px var(--accent); letter-spacing:2px; margin-bottom:4px; }
  h1 span { color:var(--danger); }
  .sub { font-size:.75rem; color:#7fa; opacity:.6; margin-bottom:18px; }

  .topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
  #all-land {
      font-family:'Orbitron',sans-serif; font-weight:900; font-size:.9rem; letter-spacing:2px;
      color:#fff; background:rgba(255,45,85,.14); border:2px solid var(--danger);
      border-radius:8px; padding:14px 24px; cursor:pointer; white-space:nowrap;
      box-shadow:0 0 18px rgba(255,45,85,.35); transition:.15s;
  }
  #all-land:hover  { background:var(--danger); box-shadow:0 0 30px var(--danger); }
  #all-land:active { transform:scale(.95); }
  #all-land:disabled { opacity:.5; cursor:wait; }
  #all-land-msg { font-size:.68rem; color:#9fc; margin-top:6px; text-align:right; white-space:pre-wrap; }
  #all-land-msg.err { color:var(--danger); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }

  .card { background:var(--panel); border:1px solid rgba(0,229,255,.25); border-radius:8px; padding:16px;
          backdrop-filter:blur(4px); box-shadow:0 0 18px rgba(0,0,0,.4); }
  .card.offline { opacity:.55; border-color:rgba(255,255,255,.12); }
  .row { display:flex; align-items:center; justify-content:space-between; gap:10px; }
  .name { font-family:'Orbitron',sans-serif; font-weight:700; font-size:1.05rem; letter-spacing:1px; }
  .dot { width:11px; height:11px; border-radius:50%; display:inline-block; margin-right:7px; box-shadow:0 0 8px currentColor; }
  .d-fly{color:var(--ok);} .d-idle{color:var(--accent);} .d-nodr{color:var(--warn);} .d-off{color:#556;}
  .state { font-size:.72rem; letter-spacing:1px; }

  .meta { display:flex; gap:14px; margin:12px 0; font-size:.72rem; color:#9fc; flex-wrap:wrap; }
  .meta b { color:#fff; }
  .bat { margin:8px 0; }
  .bar { height:9px; border-radius:5px; background:rgba(255,255,255,.1); overflow:hidden; }
  .bar > i { display:block; height:100%; background:var(--ok); transition:width .4s; }
  .bar.low > i { background:var(--danger); }

  .link { display:flex; gap:8px; align-items:center; margin:10px 0; font-size:.72rem; }
  .link input { flex:1; background:#0a121c; border:1px solid rgba(0,229,255,.3); color:var(--accent);
                padding:6px 8px; border-radius:4px; font-family:'Share Tech Mono',monospace; }

  .btns { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
  button { font-family:'Orbitron',sans-serif; font-weight:700; font-size:.68rem; letter-spacing:1px;
           cursor:pointer; border:1px solid; border-radius:5px; padding:8px 11px; background:transparent;
           color:#dfefff; transition:.15s; }
  button:hover { background:rgba(255,255,255,.08); }
  button:active { transform:scale(.94); }
  .b-go{border-color:var(--ok);color:var(--ok);} .b-stop{border-color:var(--danger);color:var(--danger);}
  .b-re{border-color:var(--warn);color:var(--warn);} .b-alt{border-color:var(--accent);color:var(--accent);}

  .wifi { margin-top:12px; border-top:1px dashed rgba(255,255,255,.12); padding-top:11px; display:none; }
  .wifi.open { display:block; }
  .wifi select, .wifi input { width:100%; margin:5px 0; background:#0a121c; border:1px solid rgba(0,229,255,.3);
           color:#dfefff; padding:7px; border-radius:4px; font-family:'Share Tech Mono',monospace; font-size:.75rem; }
  .msg { font-size:.68rem; color:#9fc; margin-top:6px; white-space:pre-wrap; max-height:80px; overflow:auto; }
  .err { color:var(--danger); }

  /* Live-tailing log pane */
  .logbox {
      display:none; margin-top:9px; padding:8px 10px; border-radius:5px;
      background:#050b12; border:1px solid rgba(0,229,255,.22);
      font-family:'Share Tech Mono',monospace; font-size:.64rem; line-height:1.35;
      color:#8fb8c9; white-space:pre-wrap; word-break:break-word;
      max-height:190px; overflow-y:auto;
  }
  .logbox.open { display:block; }
  .b-live { border-color:var(--ok) !important; color:var(--ok) !important;
            box-shadow:0 0 10px rgba(0,255,136,.35); }
  button.copied { border-color:var(--ok) !important; color:var(--ok) !important; }
</style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>DRONE<span>//</span>FLEET</h1>
      <div class="sub" id="clock">connecting…</div>
    </div>
    <div style="text-align:right">
      <button id="all-land">⛔ ALL LAND</button>
      <div id="all-land-msg"></div>
    </div>
  </div>
  <div class="grid" id="grid"></div>

<script>
const grid = document.getElementById("grid");
// Cards are created ONCE and then updated in place. (Rebuilding them on every poll
// used to wipe the Wi-Fi scan list / typed password while you were using them.)
const cards = new Map();   // slave name -> element refs

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

// navigator.clipboard exists ONLY in a secure context (https, or localhost). Opening this
// dashboard at http://<lan-ip>:5050 leaves it undefined, so the modern API silently throws.
// Fall back to the legacy execCommand path, and always show whether it actually worked.
async function copyText(text, btn) {
  let ok = false;
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (_) { ok = false; }

  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;left:0;opacity:0;";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);          // iOS needs the explicit range
    try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
    ta.remove();
  }

  if (btn) {                                        // never fail silently again
    const label = btn.textContent;
    btn.textContent = ok ? "COPIED" : "CTRL+C";
    btn.classList.toggle("copied", ok);
    setTimeout(() => { btn.textContent = label; btn.classList.remove("copied"); }, 1200);
  }
  return ok;
}

function dotClass(s) {
  if (!s.online || !s.running) return "d-off";
  if (s.flying) return "d-fly";
  if (s.drone_connected) return "d-idle";
  return "d-nodr";
}
function stateText(s) {
  if (!s.online)  return "AGENT OFFLINE";
  if (!s.running) return "STOPPED";
  if (s.flying)   return "FLYING";
  if (s.drone_connected) return "READY";
  return "NO DRONE";
}

// Build a card's DOM once and wire its buttons; never rebuilt afterwards.
function makeCard(s) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
      <div class="row">
        <div class="name"><span class="dot">●</span><span class="c-name"></span></div>
        <div class="state"></div>
      </div>
      <div class="meta">
        <div>DRONE: <b class="c-drone">—</b></div>
        <div>SSID: <b class="c-ssid">—</b></div>
        <div>IP: <b class="c-ip">—</b></div>
      </div>
      <div class="bat">
        <div class="row" style="font-size:.7rem"><span>BATTERY</span><b class="c-battxt">--</b></div>
        <div class="bar"><i style="width:0%"></i></div>
      </div>
      <div class="link">
        <input class="c-link" readonly value="">
        <button class="b-alt c-copy">COPY</button>
        <button class="b-alt c-open">OPEN</button>
      </div>
      <div class="btns">
        <button class="b-go   c-start">START</button>
        <button class="b-stop c-stop">STOP</button>
        <button class="b-re   c-restart">RESTART</button>
        <button class="b-stop c-land">LAND</button>
        <button class="b-alt  c-logs">LOG</button>
        <button class="b-alt  c-wifitoggle">WI-FI ▾</button>
      </div>
      <div class="msg c-err"></div>
      <pre class="logbox c-log"></pre>
      <div class="wifi">
        <div class="btns"><button class="b-alt c-scan">SCAN</button></div>
        <select class="c-ssidsel"><option value="">— pick a network —</option></select>
        <input class="c-pwd" type="text" placeholder="password (blank = open network)">
        <div class="btns"><button class="b-go c-connect">CONNECT DRONE WI-FI</button></div>
        <div class="msg c-msg"></div>
      </div>`;

  const q = sel => el.querySelector(sel);
  const c = {
    el, name: s.name, link: "",
    dot: q(".dot"), state: q(".state"),
    drone: q(".c-drone"), ssid: q(".c-ssid"), ip: q(".c-ip"),
    batTxt: q(".c-battxt"), bar: q(".bar"), barFill: q(".bar > i"),
    linkEl: q(".c-link"), copy: q(".c-copy"), open: q(".c-open"),
    wifi: q(".wifi"), sel: q(".c-ssidsel"), pwd: q(".c-pwd"), msg: q(".c-msg"),
    err: q(".c-err"), pinned: false,   // pinned = an action message is showing; don't overwrite it
    logEl: q(".c-log"), logBtn: q(".c-logs"), logOpen: false,
  };
  q(".c-name").textContent = s.name;

  q(".c-start").onclick      = () => act(c, "start");
  q(".c-stop").onclick       = () => act(c, "stop");
  q(".c-restart").onclick    = () => act(c, "restart");
  q(".c-land").onclick       = () => act(c, "land");
  c.logBtn.onclick           = () => toggleLogs(c);
  q(".c-wifitoggle").onclick = () => c.wifi.classList.toggle("open");
  q(".c-scan").onclick       = () => scan(c);
  q(".c-connect").onclick    = () => connectWifi(c);
  c.copy.onclick = async () => {
    if (!c.link) return;
    const ok = await copyText(c.link, c.copy);
    if (!ok) { c.linkEl.focus(); c.linkEl.select(); }   // last resort: let them hit Ctrl+C
  };
  c.open.onclick = () => { if (c.link) window.open(c.link, "_blank"); };

  grid.appendChild(el);
  cards.set(s.name, c);
  return c;
}

// Refresh only the volatile fields — the Wi-Fi panel keeps its scan list + password.
function update(c, s) {
  c.el.className  = "card" + (s.online ? "" : " offline");
  c.dot.className = "dot " + dotClass(s);
  c.state.textContent = stateText(s);
  c.drone.textContent = s.drone_connected ? "LINKED" : "—";
  c.ssid.textContent  = s.ssid || "—";
  c.ip.textContent    = s.host;

  const bat = parseInt(s.battery);
  const pct = isNaN(bat) ? 0 : Math.max(0, Math.min(100, bat));
  c.batTxt.textContent  = isNaN(bat) ? "--" : bat + "%";
  c.barFill.style.width = pct + "%";
  c.bar.classList.toggle("low", !isNaN(bat) && bat <= 20);

  c.link = s.link || "";
  const shown = c.link || "(no player link yet)";
  if (c.linkEl.value !== shown) c.linkEl.value = shown;
  c.linkEl.title  = c.link;
  c.copy.disabled = !c.link;
  c.open.disabled = !c.link;

  // Surface whatever is actually wrong, instead of failing silently.
  if (!c.pinned) {
    let e = "";
    if (!s.online)            e = "agent unreachable: " + (s.error || "?") + "  (firewall? token? agent running?)";
    else if (s.app_ok === false) e = "controller file NOT FOUND on slave:\n" + (s.app || "") + "\n→ fix APP_FILE in agent.py";
    else if (s.pyhula_ok === false) e = "this Python cannot import pyhula:\n" + (s.python || "") +
                                       "\n→ run the agent with your venv python";
    else if (s.agent_error)   e = s.agent_error;
    c.err.textContent = e;
    c.err.className   = e ? "msg err" : "msg";
  }
}

// LOG is a toggle: while open, the pane live-tails that slave's controller output.
function toggleLogs(c) {
  c.logOpen = !c.logOpen;
  c.logEl.classList.toggle("open", c.logOpen);
  c.logBtn.classList.toggle("b-live", c.logOpen);
  c.logBtn.textContent = c.logOpen ? "LOG ■" : "LOG";
  if (c.logOpen) { c.logEl.textContent = "loading log…"; pollLog(c); }
}

async function pollLog(c) {
  if (!c.logOpen) return;
  const r = await api(`/api/${c.name}/logs`);
  if (!c.logOpen) return;                       // toggled off while the request was in flight
  const text = (r.ok && r.lines && r.lines.length)
    ? r.lines.join("\n")
    : (r.ok ? "(no output yet — press START)" : "log failed: " + (r.error || "?"));

  if (text === c.logEl.textContent) return;     // nothing new; don't disturb scroll
  const box = c.logEl;
  // Only auto-scroll if the user is already parked at the bottom, so scrolling
  // back to read something doesn't get yanked away on the next tick.
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 28;
  box.textContent = text;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// Poll faster than the status refresh, but only for panes that are actually open.
setInterval(() => {
  for (const c of cards.values()) if (c.logOpen) pollLog(c);
}, 1200);

// ─── ALL LAND: fire at every drone in parallel. No confirm dialog — landing is the
//     safe direction, and an emergency control must not have friction. ───
const allLandBtn = document.getElementById("all-land");
const allLandMsg = document.getElementById("all-land-msg");

allLandBtn.onclick = async () => {
  allLandBtn.disabled = true;
  allLandMsg.className = "";
  allLandMsg.textContent = "landing all drones…";
  try {
    const r = await api("/api/land_all", { method: "POST" });
    const lines = (r.results || []).map(x => `${x.name}: ${x.ok ? "LANDED" : "FAILED — " + (x.error || "?")}`);
    allLandMsg.textContent = lines.join("\n") || "no slaves configured";
    allLandMsg.className = r.ok ? "" : "err";
  } catch (e) {
    allLandMsg.textContent = "ALL LAND failed: " + e;
    allLandMsg.className = "err";
  }
  allLandBtn.disabled = false;
  refresh();
  setTimeout(() => { allLandMsg.textContent = ""; allLandMsg.className = ""; }, 8000);
};

async function refresh() {
  try {
    const fleet = await api("/api/fleet");
    for (const s of fleet) update(cards.get(s.name) || makeCard(s), s);

    const names = new Set(fleet.map(s => s.name));   // drop slaves removed from fleet.json
    for (const [n, c] of cards) if (!names.has(n)) { c.el.remove(); cards.delete(n); }

    document.getElementById("clock").textContent =
      fleet.length + " laptop(s) · updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("clock").textContent = "dashboard error: " + e;
  }
}

async function act(c, action) {
  c.pinned = true;
  c.err.className = "msg"; c.err.textContent = action + "…";
  const r = await api(`/api/${c.name}/${action}`, { method: "POST" });
  if (r.ok) {
    c.err.textContent = action + " ok";
    c.err.className = "msg";
    c.pinned = false;
  } else {
    c.err.textContent = action + " FAILED: " + (r.error || "?");
    c.err.className = "msg err";
    setTimeout(() => { c.pinned = false; }, 10000);
  }
  refresh();
}

async function scan(c) {
  c.msg.className = "msg"; c.msg.textContent = "scanning…";
  const r = await api(`/api/${c.name}/wifi_scan`);
  if (r.ok && r.ssids) {
    const keep = c.sel.value;                        // don't lose the user's current pick
    c.sel.innerHTML = '<option value="">— pick a network —</option>';
    for (const s of r.ssids) {
      const o = document.createElement("option");
      o.value = s; o.textContent = s + (s === r.current ? "  (connected)" : "");
      c.sel.appendChild(o);
    }
    if (keep) c.sel.value = keep;
    c.msg.textContent = r.ssids.length + " network(s) found";
  } else {
    c.msg.className = "msg err";
    c.msg.textContent = "scan failed: " + (r.error || "?");
  }
}

async function connectWifi(c) {
  const ssid = c.sel.value, pwd = c.pwd.value;
  if (!ssid) { c.msg.className = "msg err"; c.msg.textContent = "pick an SSID first"; return; }
  c.msg.className = "msg"; c.msg.textContent = "connecting to " + ssid + "…";
  const r = await api(`/api/${c.name}/connect_wifi`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ssid, password: pwd })
  });
  c.msg.className   = r.ok ? "msg" : "msg err";
  c.msg.textContent = r.ok ? ("→ " + ssid + "\n" + (r.output || "")) : ("failed: " + (r.error || "?"));
}

refresh();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(f"[MASTER] Dashboard -> http://localhost:{DASH_PORT}")
    app.run(host="0.0.0.0", port=DASH_PORT, threaded=True)
