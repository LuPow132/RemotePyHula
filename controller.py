from flask import Flask, render_template_string
from flask_sock import Sock
import json, time, threading
import pyhula

app = Flask(__name__)
sock = Sock(app)

# ─────────────────────────────────────────
#  Drone connection
# ─────────────────────────────────────────
api = pyhula.UserApi()
connected = False
flying = False

for i in range(5):
    print(f"📡 Connection attempt {i+1}/5...")
    if api.connect():
        print("✅ Connected to drone!")
        connected = True
        break
    time.sleep(1)

if not connected:
    print("❌ Could not connect to drone. Running in mock mode.")

# ─────────────────────────────────────────
#  RC state — updated by WS, consumed by tick loop
# ─────────────────────────────────────────
rc_state = {
    "forward": 0,   # -100 to 100
    "right":   0,   # -100 to 100
    "up":      0,   # -100 to 100
    "yaw":     0,   # -100 to 100
}
rc_lock = threading.Lock()

TICK_MS   = 50          # send RC command every 50ms
STEP_MAX  = 20          # max step size sent to drone per tick
DEADZONE  = 8           # ignore tiny joystick noise

def rc_tick_loop():
    """Continuously sends movement commands at fixed interval while flying."""
    while True:
        time.sleep(TICK_MS / 1000)
        if not flying or not connected:
            continue
        with rc_lock:
            fwd = rc_state["forward"]
            rgt = rc_state["right"]
            up  = rc_state["up"]
            yaw = rc_state["yaw"]

        def scale(v):
            """Scale -100..100 joystick to 0..STEP_MAX drone step, with deadzone."""
            if abs(v) < DEADZONE:
                return 0
            return max(1, int(abs(v) / 100 * STEP_MAX))

        try:
            if abs(fwd) >= DEADZONE:
                if fwd > 0: api.single_fly_forward(scale(fwd))
                else:       api.single_fly_back(scale(fwd))
            if abs(rgt) >= DEADZONE:
                if rgt > 0: api.single_fly_right(scale(rgt))
                else:       api.single_fly_left(scale(rgt))
            if abs(up) >= DEADZONE:
                if up > 0:  api.single_fly_up(scale(up))
                else:       api.single_fly_down(scale(up))
            if abs(yaw) >= DEADZONE:
                if yaw > 0: api.single_fly_turnright(scale(yaw))
                else:       api.single_fly_turnleft(scale(yaw))
        except Exception as e:
            print(f"[RC] Error: {e}")

tick_thread = threading.Thread(target=rc_tick_loop, daemon=True)
tick_thread.start()

# ─────────────────────────────────────────
#  HTML UI
# ─────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>DRONE//CTRL</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:      #080c10;
    --panel:   #0d1520;
    --border:  #1a2e45;
    --accent:  #00e5ff;
    --danger:  #ff2d55;
    --ok:      #00ff88;
    --dim:     #2a4060;
    --text:    #cde8ff;
    --mono:    'Share Tech Mono', monospace;
    --display: 'Orbitron', sans-serif;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    overflow-x: hidden;
  }

  /* Scanline overlay */
  body::before {
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,0,0,0.07) 2px,
      rgba(0,0,0,0.07) 4px
    );
    pointer-events: none;
    z-index: 999;
  }

  /* Grid bg */
  body::after {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(rgba(0,229,255,0.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,229,255,0.03) 1px, transparent 1px);
    background-size: 40px 40px;
    pointer-events: none;
    z-index: 0;
  }

  /* ── HEADER ── */
  header {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 18px 24px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--border);
  }

  .logo {
    font-family: var(--display);
    font-size: 1.3rem;
    font-weight: 900;
    letter-spacing: 4px;
    color: var(--accent);
    text-shadow: 0 0 20px var(--accent);
  }
  .logo span { color: var(--danger); }

  #conn-status {
    font-size: 0.7rem;
    letter-spacing: 2px;
    padding: 4px 12px;
    border-radius: 2px;
    border: 1px solid var(--dim);
    color: var(--dim);
    transition: all 0.3s;
  }
  #conn-status.live {
    border-color: var(--ok);
    color: var(--ok);
    text-shadow: 0 0 8px var(--ok);
    box-shadow: 0 0 12px rgba(0,255,136,0.15);
  }
  #conn-status.dead {
    border-color: var(--danger);
    color: var(--danger);
  }

  /* ── TELEMETRY BAR ── */
  #telem {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 8px 24px;
    display: flex;
    gap: 24px;
    font-size: 0.68rem;
    letter-spacing: 1px;
    color: var(--dim);
    border-bottom: 1px solid var(--border);
  }
  #telem span { color: var(--accent); }
  #fly-state { margin-left: auto; }
  #fly-state.airborne { color: var(--ok); text-shadow: 0 0 8px var(--ok); }

  /* ── MAIN LAYOUT ── */
  main {
    position: relative; z-index: 1;
    width: 100%; max-width: 900px;
    padding: 20px 16px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 20px;
  }

  /* ── ACTION BUTTONS ── */
  .action-row {
    display: flex;
    gap: 16px;
    width: 100%;
    max-width: 500px;
  }

  .btn {
    flex: 1;
    padding: 14px;
    border: 1px solid;
    background: transparent;
    font-family: var(--display);
    font-size: 0.75rem;
    font-weight: 700;
    letter-spacing: 3px;
    cursor: pointer;
    border-radius: 2px;
    transition: all 0.15s;
    position: relative;
    overflow: hidden;
  }
  .btn::after {
    content: '';
    position: absolute; inset: 0;
    background: currentColor;
    opacity: 0;
    transition: opacity 0.15s;
  }
  .btn:active::after { opacity: 0.15; }

  .btn-takeoff {
    border-color: var(--ok);
    color: var(--ok);
    box-shadow: 0 0 16px rgba(0,255,136,0.1);
  }
  .btn-takeoff:hover {
    background: rgba(0,255,136,0.08);
    box-shadow: 0 0 24px rgba(0,255,136,0.25);
  }

  .btn-land {
    border-color: var(--danger);
    color: var(--danger);
    box-shadow: 0 0 16px rgba(255,45,85,0.1);
  }
  .btn-land:hover {
    background: rgba(255,45,85,0.08);
    box-shadow: 0 0 24px rgba(255,45,85,0.25);
  }

  /* ── JOYSTICK AREA ── */
  .sticks {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 40px;
    width: 100%;
  }

  .stick-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
  }

  .stick-label {
    font-size: 0.6rem;
    letter-spacing: 2px;
    color: var(--dim);
  }
  .stick-label span {
    color: var(--accent);
    font-size: 0.65rem;
  }

  .joystick-zone {
    width: 160px; height: 160px;
    border-radius: 50%;
    background: var(--panel);
    border: 1px solid var(--border);
    position: relative;
    touch-action: none;
    box-shadow:
      inset 0 0 30px rgba(0,0,0,0.5),
      0 0 0 1px rgba(0,229,255,0.05),
      0 4px 24px rgba(0,0,0,0.4);
  }

  /* crosshair lines */
  .joystick-zone::before,
  .joystick-zone::after {
    content: '';
    position: absolute;
    background: var(--border);
  }
  .joystick-zone::before {
    width: 1px; height: 100%;
    left: 50%; top: 0;
  }
  .joystick-zone::after {
    height: 1px; width: 100%;
    top: 50%; left: 0;
  }

  .knob {
    width: 52px; height: 52px;
    border-radius: 50%;
    background: radial-gradient(circle at 35% 35%, #1a3a5a, #0a1a2a);
    border: 2px solid var(--accent);
    box-shadow:
      0 0 12px rgba(0,229,255,0.4),
      0 0 0 4px rgba(0,229,255,0.08);
    position: absolute;
    top: 54px; left: 54px;
    pointer-events: none;
    transition: box-shadow 0.1s;
    z-index: 2;
  }
  .joystick-zone.active .knob {
    box-shadow:
      0 0 20px rgba(0,229,255,0.7),
      0 0 0 6px rgba(0,229,255,0.15);
  }

  /* ── RC READOUT ── */
  .rc-readout {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    width: 100%;
    max-width: 500px;
  }

  .rc-cell {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 2px;
    padding: 8px;
    text-align: center;
  }
  .rc-cell-label {
    font-size: 0.55rem;
    letter-spacing: 2px;
    color: var(--dim);
    margin-bottom: 3px;
  }
  .rc-cell-val {
    font-family: var(--display);
    font-size: 0.95rem;
    color: var(--accent);
  }

  /* ── CORNER DECORATIONS ── */
  .corner-dec {
    position: relative;
    padding: 4px;
  }
  .corner-dec::before,
  .corner-dec::after {
    content: '';
    position: absolute;
    width: 10px; height: 10px;
    border-color: var(--accent);
    border-style: solid;
    opacity: 0.4;
  }
  .corner-dec::before {
    top: 0; left: 0;
    border-width: 1px 0 0 1px;
  }
  .corner-dec::after {
    bottom: 0; right: 0;
    border-width: 0 1px 1px 0;
  }

  @media (max-width: 480px) {
    .joystick-zone { width: 130px; height: 130px; }
    .knob { width: 44px; height: 44px; top: 43px; left: 43px; }
    .sticks { gap: 20px; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">DRONE<span>//</span>CTRL</div>
  <div id="conn-status">OFFLINE</div>
</header>

<div id="telem">
  <div>FWD: <span id="t-fwd">0</span></div>
  <div>RGT: <span id="t-rgt">0</span></div>
  <div>UP: <span id="t-up">0</span></div>
  <div>YAW: <span id="t-yaw">0</span></div>
  <div id="fly-state">GROUNDED</div>
</div>

<main>
  <div class="action-row">
    <button class="btn btn-takeoff" id="btn-takeoff">▲ TAKEOFF</button>
    <button class="btn btn-land"    id="btn-land">▼ LAND</button>
  </div>

  <div class="sticks">
    <div class="stick-wrap">
      <div class="corner-dec">
        <div class="joystick-zone" id="left-zone">
          <div class="knob" id="left-knob"></div>
        </div>
      </div>
      <div class="stick-label">THROTTLE · <span>YAW</span></div>
    </div>

    <div class="stick-wrap">
      <div class="corner-dec">
        <div class="joystick-zone" id="right-zone">
          <div class="knob" id="right-knob"></div>
        </div>
      </div>
      <div class="stick-label">PITCH · <span>ROLL</span></div>
    </div>
  </div>

  <div class="rc-readout">
    <div class="rc-cell">
      <div class="rc-cell-label">FORWARD</div>
      <div class="rc-cell-val" id="v-fwd">0</div>
    </div>
    <div class="rc-cell">
      <div class="rc-cell-label">RIGHT</div>
      <div class="rc-cell-val" id="v-rgt">0</div>
    </div>
    <div class="rc-cell">
      <div class="rc-cell-label">UP</div>
      <div class="rc-cell-val" id="v-up">0</div>
    </div>
    <div class="rc-cell">
      <div class="rc-cell-label">YAW</div>
      <div class="rc-cell-val" id="v-yaw">0</div>
    </div>
  </div>
</main>

<script>
// ── WebSocket ──
const proto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(proto + "//" + location.host + "/ws");
const statusEl  = document.getElementById("conn-status");
const flyEl     = document.getElementById("fly-state");

ws.onopen  = () => { statusEl.textContent = "LIVE"; statusEl.className = "live"; };
ws.onclose = () => { statusEl.textContent = "OFFLINE"; statusEl.className = "dead"; };

function send(obj) {
  if (ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify(obj));
}

ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.flying !== undefined) {
    flyEl.textContent = msg.flying ? "AIRBORNE" : "GROUNDED";
    flyEl.className   = msg.flying ? "airborne" : "";
  }
};

// ── Buttons ──
document.getElementById("btn-takeoff").onclick = () => send({ cmd: "takeoff" });
document.getElementById("btn-land").onclick    = () => send({ cmd: "land"    });

// ── RC state ──
const rc = { forward: 0, right: 0, up: 0, yaw: 0 };

function updateTelem() {
  document.getElementById("t-fwd").textContent  = rc.forward;
  document.getElementById("t-rgt").textContent  = rc.right;
  document.getElementById("t-up").textContent   = rc.up;
  document.getElementById("t-yaw").textContent  = rc.yaw;
  document.getElementById("v-fwd").textContent  = rc.forward;
  document.getElementById("v-rgt").textContent  = rc.right;
  document.getElementById("v-up").textContent   = rc.up;
  document.getElementById("v-yaw").textContent  = rc.yaw;
}

function sendRC() {
  send({ cmd: "rc", ...rc });
  updateTelem();
}

// ── Joystick factory ──
function makeJoystick(zoneId, knobId, onMove) {
  const zone = document.getElementById(zoneId);
  const knob = document.getElementById(knobId);
  const R = zone.offsetWidth / 2;
  const cx = R, cy = R;
  let active = false, pid = null;

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function move(ex, ey) {
    const rect = zone.getBoundingClientRect();
    let dx = ex - rect.left - cx;
    let dy = ey - rect.top  - cy;
    const dist = Math.hypot(dx, dy);
    if (dist > R - 26) { dx *= (R - 26) / dist; dy *= (R - 26) / dist; }
    knob.style.left = (cx + dx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy + dy - knob.offsetHeight / 2) + "px";
    const nx = Math.round(clamp(dx / (R - 26) * 100, -100, 100));
    const ny = Math.round(clamp(dy / (R - 26) * 100, -100, 100));
    onMove(nx, ny);
  }

  function reset() {
    knob.style.left = (cx - knob.offsetWidth  / 2) + "px";
    knob.style.top  = (cy - knob.offsetHeight / 2) + "px";
    onMove(0, 0);
  }

  zone.addEventListener("pointerdown", e => {
    active = true; pid = e.pointerId;
    zone.setPointerCapture(e.pointerId);
    zone.classList.add("active");
    move(e.clientX, e.clientY);
  });
  zone.addEventListener("pointermove", e => {
    if (active && e.pointerId === pid) move(e.clientX, e.clientY);
  });
  ["pointerup","pointercancel"].forEach(ev =>
    zone.addEventListener(ev, () => {
      active = false;
      zone.classList.remove("active");
      reset();
    })
  );

  // init knob position
  reset();
}

// Left stick  → throttle (−Y) and yaw (+X)
makeJoystick("left-zone", "left-knob", (x, y) => {
  rc.yaw     =  x;
  rc.up      = -y;   // up on stick = positive throttle
  sendRC();
});

// Right stick → pitch (−Y = forward) and roll (+X = right)
makeJoystick("right-zone", "right-knob", (x, y) => {
  rc.right   =  x;
  rc.forward = -y;
  sendRC();
});
</script>
</body>
</html>
"""

# ─────────────────────────────────────────
#  Routes
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)

@sock.route("/ws")
def websocket(ws):
    global flying
    print("[WS] Client connected")
    # send initial state
    ws.send(json.dumps({"flying": flying}))

    while True:
        data = ws.receive()
        if data is None:
            break
        try:
            msg = json.loads(data)
        except Exception:
            continue

        cmd = msg.get("cmd")

        if cmd == "takeoff" and not flying and connected:
            print("[CMD] Takeoff")
            api.single_fly_takeoff()
            flying = True
            time.sleep(2)
            ws.send(json.dumps({"flying": True}))

        elif cmd == "land" and flying and connected:
            print("[CMD] Land")
            api.single_fly_touchdown()
            flying = False
            # reset RC
            with rc_lock:
                for k in rc_state: rc_state[k] = 0
            ws.send(json.dumps({"flying": False}))

        elif cmd == "rc":
            with rc_lock:
                rc_state["forward"] = int(msg.get("forward", 0))
                rc_state["right"]   = int(msg.get("right",   0))
                rc_state["up"]      = int(msg.get("up",      0))
                rc_state["yaw"]     = int(msg.get("yaw",     0))

    print("[WS] Client disconnected")
    # Safety: stop movement on disconnect
    with rc_lock:
        for k in rc_state: rc_state[k] = 0

if __name__ == "__main__":
    print("🌐 Bridge server starting on http://0.0.0.0:8080")
    print("   Run cloudflare tunnel: cloudflared tunnel --url http://localhost:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)