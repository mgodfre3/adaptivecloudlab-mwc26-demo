"""
Dashboard Backend — Real-Time Drone Network Monitoring
======================================================
Flask + Socket.IO server that:
  1. Reads telemetry from IoT Hub's built-in Event Hub endpoint
  2. Pushes updates to the browser via WebSocket
  3. Falls back to demo mode (synthetic data) if no Event Hub connection
  4. Sends telemetry to Foundry Local on AKS Arc for edge AI analysis

Env vars (set in dashboard/.env or inherit from iot-simulation/.env):
  EVENTHUB_CONNECTION_STRING  – IoT Hub built-in Event Hub-compatible conn str
  EVENTHUB_CONSUMER_GROUP     – consumer group (default: drone-telemetry)
  DEMO_MODE                   – set to "true" to use synthetic data
  DASHBOARD_PORT              – web server port (default: 5000)
  EDGE_AI_ENDPOINT            – Foundry Local URL (default: https://localhost:8443)
  EDGE_AI_MODEL               – Model name (default: Phi-4-mini-instruct)
  EDGE_AI_API_KEY             – Foundry Local API key
  EDGE_AI_ENABLED             – set to "true" to enable edge AI analysis
  MAP_LOCATION_NAME           – display name for the demo city (default: Denver, CO)
  MAP_CENTER_LAT              – map center latitude (default: 39.7484)
  MAP_CENTER_LON              – map center longitude (default: -104.9951)
  MAP_BASE_LAT                – drone home-pad latitude (default: 39.7437)
  MAP_BASE_LON                – drone home-pad longitude (default: -104.9916)
  MAP_ZOOM                    – initial Leaflet zoom level (default: 13)
"""

import json
import os
import random
import math
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO

# ── Load config ──────────────────────────────────────────────────────────────
_here = Path(__file__).resolve().parent
_env_candidates = [_here / ".env", _here.parent / "iot-simulation" / ".env"]
for p in _env_candidates:
    if p.exists():
        load_dotenv(p)

EVENTHUB_CONN_STR = os.getenv("EVENTHUB_CONNECTION_STRING", "")
CONSUMER_GROUP = os.getenv("EVENTHUB_CONSUMER_GROUP", "drone-telemetry")
DATA_MODE = os.getenv("DATA_MODE", "cloud").lower()
MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "aio-broker-insecure.azure-iot-operations.svc")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "drone/telemetry")
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true" or (
    DATA_MODE != "edge" and not EVENTHUB_CONN_STR
)
PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DRONE_COUNT = int(os.getenv("DRONE_COUNT", "5"))

# Edge AI (Foundry Local on AKS Arc)
EDGE_AI_ENDPOINT = os.getenv("EDGE_AI_ENDPOINT", "https://localhost:8443")
EDGE_AI_MODEL = os.getenv("EDGE_AI_MODEL", "Phi-4-mini-instruct")
EDGE_AI_API_KEY = os.getenv("EDGE_AI_API_KEY", "")
EDGE_AI_ENABLED = os.getenv("EDGE_AI_ENABLED", "false").lower() == "true"
EDGE_AI_INTERVAL = int(os.getenv("EDGE_AI_INTERVAL", "15"))  # seconds between analyses

# ── Map / location config (override per demo environment) ────────────────────
MAP_LOCATION_NAME = os.getenv("MAP_LOCATION_NAME", "Denver, CO")
MAP_CENTER_LAT = float(os.getenv("MAP_CENTER_LAT", "39.7484"))
MAP_CENTER_LON = float(os.getenv("MAP_CENTER_LON", "-104.9951"))
MAP_BASE_LAT = float(os.getenv("MAP_BASE_LAT", "39.7437"))
MAP_BASE_LON = float(os.getenv("MAP_BASE_LON", "-104.9916"))
MAP_ZOOM = int(os.getenv("MAP_ZOOM", "13"))

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(16))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# In-memory latest state per drone
drone_state: dict[str, dict] = {}
ai_insights: dict = {"status": "initializing", "insights": [], "last_updated": None}
_shutdown = threading.Event()


@app.route("/")
def index():
    map_config = {
        "center_lat": MAP_CENTER_LAT,
        "center_lon": MAP_CENTER_LON,
        "zoom": MAP_ZOOM,
        "location_name": MAP_LOCATION_NAME,
    }
    return render_template("index.html", map_config=map_config)


@app.route("/api/state")
def api_state():
    """Return current state of all drones as JSON."""
    return jsonify(drone_state)


@app.route("/api/ai-insights")
def api_ai_insights():
    """Return latest AI analysis results."""
    return jsonify(ai_insights)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Interactive chat with the edge AI model about fleet status."""
    body = request.get_json(silent=True) or {}
    user_msg = (body.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    # Build fleet context so the model knows current state
    snapshot = _build_telemetry_snapshot()
    context = snapshot if snapshot else "No drones currently active."

    system_prompt = (
        "You are the Edge AI assistant for a drone fleet monitoring dashboard. "
        "You have access to real-time telemetry from the fleet. Answer user questions "
        "concisely using the fleet data provided. If no fleet data is available, say so. "
        "Keep answers under 3 sentences unless the user asks for detail."
    )

    import urllib.request, urllib.error, ssl

    url = f"{EDGE_AI_ENDPOINT}/v1/chat/completions"
    payload = json.dumps({
        "model": EDGE_AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Current fleet telemetry:\n{context}\n\nUser question: {user_msg}"},
        ],
        "temperature": 0.4,
        "max_tokens": 250,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if EDGE_AI_API_KEY:
        headers["api-key"] = EDGE_AI_API_KEY

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read())
            choices = result.get("choices", [])
            if not choices:
                return jsonify({"reply": "No response from model.", "model": EDGE_AI_MODEL})
            content = choices[0].get("message", {}).get("content", "").strip()
            return jsonify({"reply": content or "Empty response.", "model": EDGE_AI_MODEL})
    except Exception as e:
        print(f"[Chat] Error: {e}")
        # Fallback: provide a rule-based answer using current insights
        if ai_insights and ai_insights.get("summary"):
            return jsonify({
                "reply": f"(AI model unavailable — here's the latest analysis) {ai_insights['summary']}",
                "model": f"{EDGE_AI_MODEL} (offline)"
            })
        return jsonify({
            "reply": "Edge AI model is not reachable right now. Check that Foundry Local is running on the cluster.",
            "model": f"{EDGE_AI_MODEL} (offline)"
        })


@app.route("/cell-towers")
def cell_towers():
    """Self-contained cell tower coverage map (loaded in iframe overlay)."""
    return render_template("cell_towers.html",
                           center_lat=MAP_CENTER_LAT,
                           center_lon=MAP_CENTER_LON,
                           zoom=MAP_ZOOM,
                           location_name=MAP_LOCATION_NAME)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Retire all active drones and reset the fleet."""
    ids = list(drone_state.keys())
    drone_state.clear()
    for drone_id in ids:
        socketio.emit("drone_retired", {"drone_id": drone_id})
    socketio.emit("fleet_reset")
    return jsonify({"status": "reset", "drones_cleared": len(ids)})


# ── MQTT consumer (edge mode) ─────────────────────────────────────────────────

def _start_mqtt_consumer():
    """Subscribe to AIO MQTT broker and push telemetry to WebSocket."""
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print(f"[MQTT] Connection failed with reason code: {reason_code}")
            return
        topic = f"{MQTT_TOPIC_PREFIX}/#"
        client.subscribe(topic, qos=1)
        print(f"[MQTT] Subscribed to {topic}")

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            drone_id = payload.get("drone_id", "unknown")
            drone_state[drone_id] = payload
            socketio.emit("telemetry", payload)
        except Exception as e:
            print(f"[MQTT] Message error: {e}")

    print(f"[MQTT] Connecting to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}...")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="drone-dashboard")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
    except Exception as e:
        print(f"[MQTT] Failed to connect to broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}: {e}")
        return
    client.loop_forever()


# ── Event Hub consumer ───────────────────────────────────────────────────────

def _start_eventhub_consumer():
    """Read from IoT Hub's built-in Event Hub endpoint and push to WebSocket."""
    from azure.eventhub import EventHubConsumerClient

    def on_event(partition_context, event):
        if event and event.body_as_str():
            try:
                payload = json.loads(event.body_as_str())
                drone_id = payload.get("drone_id", "unknown")
                drone_state[drone_id] = payload
                socketio.emit("telemetry", payload)
            except json.JSONDecodeError:
                pass
            partition_context.update_checkpoint()

    print(f"[EventHub] Connecting to IoT Hub (consumer group: {CONSUMER_GROUP})...")
    client = EventHubConsumerClient.from_connection_string(
        EVENTHUB_CONN_STR,
        consumer_group=CONSUMER_GROUP,
    )
    with client:
        client.receive(
            on_event=on_event,
            starting_position="-1",  # latest
        )


# ── Demo-mode synthetic data generator ───────────────────────────────────────

# Demo center and base coordinates — read from env vars (see MAP_* config above)
BCN_LAT, BCN_LON = MAP_CENTER_LAT, MAP_CENTER_LON

# Base / home pad
BASE_LAT, BASE_LON = MAP_BASE_LAT, MAP_BASE_LON

# Battery thresholds
BATTERY_RETURN = 18        # % — start flying home
BATTERY_CRITICAL = 5       # % — forced landing
BATTERY_LAUNCH = 92        # % — charged enough to launch
CHARGE_RATE = 0.35         # % per tick while charging
DRAIN_RATE_MIN = 0.025     # % per tick while flying
DRAIN_RATE_MAX = 0.065     # % per tick while flying
LAUNCH_CLIMB_TICKS = 8     # ticks to reach patrol altitude after launch
LANDING_TICKS = 6          # ticks to descend at base

# Patrol waypoints — Denver, CO landmarks spread over ~8 km
PATROL_WAYPOINTS = [
    (39.7560, -104.9942),  # Coors Field / LoDo
    (39.7373, -104.9884),  # Civic Center Park
    (39.7502, -104.9497),  # City Park
    (39.6985, -104.9613),  # Washington Park
    (39.7667, -104.9701),  # RiNo (River North)
    (39.7651, -105.0153),  # Highlands
    (39.7445, -105.0063),  # Auraria Campus
    (39.7315, -104.9538),  # Cheesman Park
    (39.7167, -104.9524),  # Cherry Creek
    (39.7329, -104.9793),  # Capitol Hill
    (39.7530, -105.0002),  # Union Station
    (39.7665, -104.9773),  # Curtis Park
]

# NATO phonetic alphabet for drone callsigns — cycles through these
CALLSIGNS = [
    "Alpha", "Bravo", "Charlie", "Delta", "Echo",
    "Foxtrot", "Golf", "Hotel", "India", "Juliet",
    "Kilo", "Lima", "Mike", "November", "Oscar",
    "Papa", "Quebec", "Romeo", "Sierra", "Tango",
    "Uniform", "Victor", "Whiskey", "Xray", "Yankee", "Zulu",
]
_callsign_idx = 0  # global counter for next callsign


def _next_callsign() -> str:
    """Return the next callsign from the NATO alphabet, cycling forever."""
    global _callsign_idx
    name = CALLSIGNS[_callsign_idx % len(CALLSIGNS)]
    _callsign_idx += 1
    return name


class _DemoDrone:
    """Simulates a single drone with realistic patrol routes,
    battery-driven return-to-base, and lifecycle management."""

    def __init__(self, callsign: str, slot: int):
        self.drone_id = f"drone-{callsign.lower()}"
        self.callsign = callsign
        self.slot = slot  # fleet position (0..N-1) for replacement tracking

        # Start at base with slight offset so drones don't stack
        angle = 2 * math.pi * slot / max(DRONE_COUNT, 1)
        self.lat = BASE_LAT + 0.0004 * math.cos(angle)
        self.lon = BASE_LON + 0.0004 * math.sin(angle)
        self.alt = 0.0
        self.heading = random.uniform(0, 360)
        self.speed = 0.0
        self.battery = random.uniform(88, 100)

        # Lifecycle
        self.status = "launching"
        self._lifecycle_tick = 0  # ticks in current phase

        # Patrol route: pick 4-6 random waypoints for this drone
        self.waypoints = random.sample(
            PATROL_WAYPOINTS, random.randint(4, min(6, len(PATROL_WAYPOINTS)))
        )
        self.wp_idx = 0
        self.retired = False

    # ── Movement helpers ─────────────────────────────────────────────────

    def _distance_deg(self, lat2: float, lon2: float) -> float:
        """Euclidean distance in degrees (good enough for ~10 km scale)."""
        return math.sqrt((lat2 - self.lat) ** 2 + (lon2 - self.lon) ** 2)

    def _move_toward(self, target_lat: float, target_lon: float) -> bool:
        """Fly toward *target* at current speed. Returns True when arrived."""
        dlat = target_lat - self.lat
        dlon = target_lon - self.lon
        dist = math.sqrt(dlat ** 2 + dlon ** 2)

        if dist < 0.0004:  # ~40 m — close enough
            return True

        # degrees per tick  (speed_mps * 3 s / ~111 000 m/deg)
        step = min(self.speed * 3.0 / 111_000, dist)
        self.lat += dlat / dist * step
        self.lon += dlon / dist * step

        # Update heading to face target
        self.heading = math.degrees(math.atan2(dlon, dlat)) % 360

        # Slight realism wobble
        self.lat += random.gauss(0, 0.00003)
        self.lon += random.gauss(0, 0.00003)
        return False

    # ── Lifecycle step ───────────────────────────────────────────────────

    def step(self) -> dict:
        """Advance simulation by one tick (~3 s) and return telemetry dict."""
        self._lifecycle_tick += 1

        if self.status == "launching":
            self._step_launching()
        elif self.status in ("patrolling", "hovering"):
            self._step_patrolling()
        elif self.status == "returning":
            self._step_returning()
        elif self.status == "landing":
            self._step_landing()
        elif self.status == "charging":
            self._step_charging()

        return self._build_payload()

    def _step_launching(self):
        """Climb from base to patrol altitude."""
        self.speed = random.uniform(3, 6)
        self.alt = min(120, self.alt + random.uniform(10, 20))
        self.battery -= random.uniform(DRAIN_RATE_MIN, DRAIN_RATE_MAX)
        # Drift away from base slightly
        wp = self.waypoints[0]
        self._move_toward(wp[0], wp[1])
        if self._lifecycle_tick >= LAUNCH_CLIMB_TICKS:
            self.status = "patrolling"
            self.speed = random.uniform(8, 14)
            self._lifecycle_tick = 0
            print(f"  [Demo] {self.drone_id} launched — heading to waypoint 1")

    def _step_patrolling(self):
        """Fly between waypoints; drain battery."""
        wp = self.waypoints[self.wp_idx]
        arrived = self._move_toward(wp[0], wp[1])

        self.speed = max(4, min(18, self.speed + random.gauss(0, 0.8)))
        self.alt = max(40, min(180, self.alt + random.gauss(0, 1.5)))
        self.battery -= random.uniform(DRAIN_RATE_MIN, DRAIN_RATE_MAX)

        if arrived:
            # Move to next waypoint; occasionally hover for a few ticks
            self.wp_idx = (self.wp_idx + 1) % len(self.waypoints)
            if random.random() < 0.25:
                self.status = "hovering"
                self.speed = random.uniform(0, 2)
                self._lifecycle_tick = 0
            else:
                print(f"  [Demo] {self.drone_id} reached waypoint — next #{self.wp_idx + 1}")

        # Check battery
        if self.battery <= BATTERY_RETURN:
            self.status = "returning"
            self._lifecycle_tick = 0
            self.speed = random.uniform(12, 16)  # hurry home
            print(f"  [Demo] {self.drone_id} LOW BATTERY ({self.battery:.0f}%) — returning to base")

        # Hovering timeout — resume patrol after a short pause
        if self.status == "hovering" and self._lifecycle_tick >= random.randint(3, 8):
            self.status = "patrolling"
            self.speed = random.uniform(8, 14)
            self._lifecycle_tick = 0

    def _step_returning(self):
        """Fly back to base."""
        arrived = self._move_toward(BASE_LAT, BASE_LON)
        self.speed = max(6, min(18, self.speed + random.gauss(0, 0.5)))
        self.alt = max(30, min(180, self.alt + random.gauss(0, 1)))
        self.battery = max(0, self.battery - random.uniform(DRAIN_RATE_MIN, DRAIN_RATE_MAX))

        if arrived or self.battery <= BATTERY_CRITICAL:
            self.status = "landing"
            self._lifecycle_tick = 0
            print(f"  [Demo] {self.drone_id} reached base — landing")

    def _step_landing(self):
        """Descend to ground at base."""
        self.speed = max(0, self.speed - random.uniform(0.5, 1.5))
        self.alt = max(0, self.alt - random.uniform(12, 25))
        self.battery = max(0, self.battery - random.uniform(0.01, 0.02))
        self._move_toward(BASE_LAT, BASE_LON)

        if self._lifecycle_tick >= LANDING_TICKS or self.alt <= 0:
            self.alt = 0
            self.speed = 0
            self.status = "charging"
            self._lifecycle_tick = 0
            print(f"  [Demo] {self.drone_id} landed — charging")

    def _step_charging(self):
        """Sit at base and recharge. Mark retired when full."""
        self.speed = 0
        self.alt = 0
        self.lat = BASE_LAT + random.gauss(0, 0.00005)
        self.lon = BASE_LON + random.gauss(0, 0.00005)
        self.battery = min(100, self.battery + CHARGE_RATE)

        if self.battery >= BATTERY_LAUNCH:
            self.retired = True
            print(f"  [Demo] {self.drone_id} fully charged — retiring for replacement")

    # ── Telemetry payload ────────────────────────────────────────────────

    def _build_payload(self) -> dict:
        rsrp = random.randint(-120, -60)
        return {
            "drone_id": self.drone_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "location": {
                "latitude": round(self.lat, 6),
                "longitude": round(self.lon, 6),
                "altitude_m": round(self.alt, 1),
                "heading_deg": round(self.heading, 1),
                "speed_mps": round(self.speed, 1),
            },
            "network": {
                "signal_rsrp_dbm": rsrp,
                "signal_rsrq_db": round(random.uniform(-20, -3), 1),
                "signal_sinr_db": round(random.uniform(-5, 30), 1),
                "cell_id": random.randint(100000, 999999),
                "band": random.choice(["n77", "n78", "n79", "n260", "n261"]),
                "downlink_mbps": round(random.uniform(50, 1200), 1),
                "uplink_mbps": round(random.uniform(10, 200), 1),
                "latency_ms": round(random.uniform(1, 25), 1),
                "packet_loss_pct": round(random.uniform(0, 2), 3),
                "connected": random.choices([True, False], weights=[0.95, 0.05])[0],
            },
            "battery_pct": round(max(0, self.battery), 1),
            "status": self.status,
            "environment": {
                "temperature_c": round(random.uniform(5, 35), 1),
                "wind_speed_mps": round(random.uniform(0, 15), 1),
                "humidity_pct": round(random.uniform(30, 90), 1),
            },
        }


def _start_demo_generator():
    """Fleet manager: generate synthetic telemetry, cycle drones on battery depletion."""
    global _callsign_idx
    _callsign_idx = 0  # reset on start

    print(f"[Demo] Running in DEMO MODE — managing fleet of {DRONE_COUNT} drones")
    print(f"[Demo] Drones patrol {MAP_LOCATION_NAME}, return to base at {BATTERY_RETURN}% battery,")
    print(f"[Demo] charge to {BATTERY_LAUNCH}%, then a replacement drone launches.")

    # Seed initial fleet with staggered batteries for visual variety
    fleet: list[_DemoDrone] = []
    for slot in range(DRONE_COUNT):
        cs = _next_callsign()
        d = _DemoDrone(cs, slot)
        # Stagger initial battery so drones don't all return at once
        d.battery = random.uniform(40, 100)
        fleet.append(d)
        print(f"  [Demo] Slot {slot}: {d.drone_id} (battery {d.battery:.0f}%)")

    while not _shutdown.is_set():
        for i, d in enumerate(fleet):
            payload = d.step()
            drone_state[d.drone_id] = payload
            socketio.emit("telemetry", payload)

            # Replace retired drones
            if d.retired:
                old_id = d.drone_id
                # Remove old drone from state
                drone_state.pop(old_id, None)
                socketio.emit("drone_retired", {"drone_id": old_id})

                # Launch replacement
                cs = _next_callsign()
                new_drone = _DemoDrone(cs, d.slot)
                fleet[i] = new_drone
                print(f"  [Demo] Slot {d.slot}: {old_id} retired -> {new_drone.drone_id} launching")

        _shutdown.wait(3)


# ── Edge AI Analyzer ─────────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """You are a drone fleet analyst. Respond with ONE short sentence summarising fleet health. Example: "Fleet healthy, all drones nominal." or "Drone-alpha signal weak at -105 dBm, drone-charlie battery low at 18%." Do NOT output JSON or bullet points."""


def _build_telemetry_snapshot() -> str:
    """Build a compact text summary of drone telemetry for the AI model.

    Instead of raw JSON (which the small model tends to echo), we send a
    concise text description so the model focuses on *analysis*.
    """
    if not drone_state:
        return ""
    lines = []
    for did, d in drone_state.items():
        net = d.get("network", {})
        bat = d.get("battery_pct", 100)
        status = d.get("status", "unknown")
        rsrp = net.get("signal_rsrp_dbm", -80)
        sinr = net.get("signal_sinr_db", 20)
        lat_ms = net.get("latency_ms", 5)
        loss = net.get("packet_loss_pct", 0)
        dl = net.get("downlink_mbps", 0)
        lines.append(
            f"{did} [{status}]: rsrp={rsrp}dBm sinr={sinr}dB lat={lat_ms}ms loss={loss}% dl={dl}Mbps bat={bat}%"
        )
    return "\n".join(lines)


def _call_edge_ai(prompt: str) -> str | None:
    """Call Foundry Local and return the model's one-sentence summary text.

    We no longer ask the model for JSON — just a short natural-language
    summary.  Rule-engine insights are generated separately.
    """
    import urllib.request
    import urllib.error
    import ssl

    url = f"{EDGE_AI_ENDPOINT}/v1/chat/completions"
    body = json.dumps({
        "model": EDGE_AI_MODEL,
        "messages": [
            {"role": "system", "content": _AI_SYSTEM_PROMPT},
            {"role": "user", "content": f"Drone fleet readings:\n{prompt}\nOne sentence summary:"},
        ],
        "temperature": 0.3,
        "max_tokens": 120,
    }).encode()

    headers = {"Content-Type": "application/json"}
    if EDGE_AI_API_KEY:
        headers["api-key"] = EDGE_AI_API_KEY

    # Foundry Local uses self-signed TLS — skip verification for cluster-internal calls
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            result = json.loads(resp.read())
            choices = result.get("choices", [])
            if not choices:
                print("[EdgeAI] No choices in response")
                return None
            content = choices[0].get("message", {}).get("content", "")
            if not content:
                content = choices[0].get("delta", {}).get("content", "")
            content = content.strip().strip('"').strip("'")
            # Remove markdown fences if the model wrapped output
            if content.startswith("```"):
                content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3].strip()
            print(f"[EdgeAI] Summary: {content[:200]}")
            return content if content else None
    except (urllib.error.URLError, Exception) as e:
        print(f"[EdgeAI] Error calling model: {e}")
        return None


def _generate_demo_insights() -> dict:
    """Generate plausible AI insights in demo mode without calling the model."""
    insights = []
    for did, d in drone_state.items():
        net = d.get("network", {})
        rsrp = net.get("signal_rsrp_dbm", 0)
        lat_ms = net.get("latency_ms", 0)
        loss = net.get("packet_loss_pct", 0)
        bat = d.get("battery_pct", 100)

        if rsrp < -100:
            insights.append({
                "type": "coverage",
                "severity": "warning",
                "drone_id": did,
                "title": "Weak signal detected",
                "detail": f"{did} reporting RSRP {rsrp} dBm — potential coverage gap in current sector.",
            })
        if bat < 30:
            mins_left = max(1, int(bat / 0.08))
            insights.append({
                "type": "battery",
                "severity": "critical" if bat < 15 else "warning",
                "drone_id": did,
                "title": "Low battery alert",
                "detail": f"{did} at {bat:.0f}% — estimated {mins_left} min remaining at current drain rate.",
            })
        if lat_ms > 15:
            insights.append({
                "type": "performance",
                "severity": "warning",
                "drone_id": did,
                "title": "Elevated latency",
                "detail": f"{did} latency {lat_ms:.1f} ms — above 15 ms threshold, possible congestion.",
            })
        if loss > 1:
            insights.append({
                "type": "anomaly",
                "severity": "warning",
                "drone_id": did,
                "title": "Packet loss spike",
                "detail": f"{did} experiencing {loss:.2f}% packet loss — investigating.",
            })

    # Fleet-wide predictive insight
    if drone_state:
        avg_bat = sum(d.get("battery_pct", 100) for d in drone_state.values()) / len(drone_state)
        avg_rsrp = sum(d.get("network", {}).get("signal_rsrp_dbm", -80) for d in drone_state.values()) / len(drone_state)
        insights.append({
            "type": "prediction",
            "severity": "info",
            "drone_id": None,
            "title": "Fleet endurance forecast",
            "detail": f"Fleet avg battery {avg_bat:.0f}% — estimated {int(avg_bat / 0.08)} min total flight time remaining.",
        })
        if avg_rsrp < -90:
            insights.insert(0, {
                "type": "coverage",
                "severity": "warning",
                "drone_id": None,
                "title": "Fleet-wide signal degradation",
                "detail": f"Average RSRP {avg_rsrp:.0f} dBm across fleet — monitoring for coverage gap pattern.",
            })

    # Trim to max 5
    insights = insights[:5]
    if not insights:
        insights.append({
            "type": "performance",
            "severity": "info",
            "drone_id": None,
            "title": "All systems nominal",
            "detail": "Fleet operating within normal parameters. No anomalies detected.",
        })

    fleet_status = "healthy"
    if any(i["severity"] == "critical" for i in insights):
        fleet_status = "critical"
    elif any(i["severity"] == "warning" for i in insights):
        fleet_status = "degraded"

    return {
        "fleet_status": fleet_status,
        "summary": f"{len(drone_state)} drones active — {'all nominal' if fleet_status == 'healthy' else 'issues detected'}.",
        "insights": insights,
    }


def _start_ai_analyzer():
    """Periodically analyze fleet telemetry via the edge AI model."""
    global ai_insights
    print(f"[EdgeAI] Analyzer started — endpoint: {EDGE_AI_ENDPOINT}, model: {EDGE_AI_MODEL}")
    print(f"[EdgeAI] Analysis interval: {EDGE_AI_INTERVAL}s, enabled: {EDGE_AI_ENABLED}")
    print(f"[EdgeAI] Platform: Foundry Local on AKS Arc (Azure Local)")
    if EDGE_AI_API_KEY:
        print(f"[EdgeAI] API key: ****{EDGE_AI_API_KEY[-4:]}")

    # Wait for some telemetry data to accumulate
    while not _shutdown.is_set() and not drone_state:
        _shutdown.wait(2)

    while not _shutdown.is_set():
        try:
            # Always use the rule engine for reliable, detailed insights
            result = _generate_demo_insights()
            result["last_updated"] = datetime.now(timezone.utc).isoformat()

            if EDGE_AI_ENABLED:
                # Emit rule-engine insights immediately (don't wait for model)
                result["status"] = "analyzing"
                result["model"] = EDGE_AI_MODEL
                result["endpoint"] = EDGE_AI_ENDPOINT
                ai_insights = result
                socketio.emit("ai_insights", ai_insights)

                # Now try to overlay the AI model's natural-language summary
                snapshot = _build_telemetry_snapshot()
                if snapshot:
                    ai_summary = _call_edge_ai(snapshot)
                    if ai_summary:
                        result["summary"] = ai_summary
                        result["status"] = "connected"
                        print(f"[EdgeAI] Analysis complete — {result['fleet_status']}, {len(result.get('insights', []))} insights")
                    else:
                        result["status"] = "fallback"
                        result["model"] = f"{EDGE_AI_MODEL} (fallback→rules)"
                        print("[EdgeAI] Model unavailable, using rule engine only")
                    result["last_updated"] = datetime.now(timezone.utc).isoformat()
            else:
                result["status"] = "demo"
                result["model"] = "rule-engine (demo)"
                result["endpoint"] = "local"

            ai_insights = result
            socketio.emit("ai_insights", ai_insights)
        except Exception as e:
            print(f"[EdgeAI] Error: {e}")
            ai_insights["status"] = "error"

        _shutdown.wait(EDGE_AI_INTERVAL)


# ── Startup ──────────────────────────────────────────────────────────────────

def _start_background():
    if DATA_MODE == "edge":
        t = threading.Thread(target=_start_mqtt_consumer, daemon=True)
    elif DEMO_MODE:
        t = threading.Thread(target=_start_demo_generator, daemon=True)
    else:
        t = threading.Thread(target=_start_eventhub_consumer, daemon=True)
    t.start()

    # Start AI analyzer thread
    ai_thread = threading.Thread(target=_start_ai_analyzer, daemon=True)
    ai_thread.start()


@socketio.on("connect")
def handle_connect():
    """Send current state to newly connected client."""
    for payload in drone_state.values():
        socketio.emit("telemetry", payload)
    if ai_insights.get("insights"):
        socketio.emit("ai_insights", ai_insights)


if __name__ == "__main__":
    if DATA_MODE == "edge":
        mode = "MQTT (edge)"
    elif DEMO_MODE:
        mode = "DEMO"
    else:
        mode = "EVENT HUB"
    ai_mode = "EDGE AI" if EDGE_AI_ENABLED else "DEMO RULES"
    print(f"{'='*60}")
    print(f"  Drone Network Monitoring Dashboard — MWC 2026")
    print(f"  Telemetry : {mode}   Port: {PORT}   Drones: {DRONE_COUNT}")
    print(f"  Edge AI   : {ai_mode} → {EDGE_AI_ENDPOINT} ({EDGE_AI_MODEL})")
    print(f"{'='*60}")
    print()
    _start_background()
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
