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
  EDGE_AI_MODEL               – Model name (default: Phi-3-mini-4k-instruct-cuda-gpu:1)
  EDGE_AI_API_KEY             – Foundry Local API key
  EDGE_AI_ENABLED             – set to "true" to enable edge AI analysis
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
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

# ── Load config ──────────────────────────────────────────────────────────────
_here = Path(__file__).resolve().parent
_env_candidates = [_here / ".env", _here.parent / "iot-simulation" / ".env"]
for p in _env_candidates:
    if p.exists():
        load_dotenv(p)

EVENTHUB_CONN_STR = os.getenv("EVENTHUB_CONNECTION_STRING", "")
CONSUMER_GROUP = os.getenv("EVENTHUB_CONSUMER_GROUP", "drone-telemetry")
DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() == "true" or not EVENTHUB_CONN_STR
PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
DRONE_COUNT = int(os.getenv("DRONE_COUNT", "5"))

# Edge AI (Foundry Local on AKS Arc)
EDGE_AI_ENDPOINT = os.getenv("EDGE_AI_ENDPOINT", "https://localhost:8443")
EDGE_AI_MODEL = os.getenv("EDGE_AI_MODEL", "Phi-3-mini-4k-instruct-cuda-gpu:1")
EDGE_AI_API_KEY = os.getenv("EDGE_AI_API_KEY", "")
EDGE_AI_ENABLED = os.getenv("EDGE_AI_ENABLED", "false").lower() == "true"
EDGE_AI_INTERVAL = int(os.getenv("EDGE_AI_INTERVAL", "15"))  # seconds between analyses

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
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    """Return current state of all drones as JSON."""
    return jsonify(drone_state)


@app.route("/api/ai-insights")
def api_ai_insights():
    """Return latest AI analysis results."""
    return jsonify(ai_insights)


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

# Barcelona, Spain — Fira Gran Via / MWC venue area
BCN_LAT, BCN_LON, RADIUS = 41.3574, 2.1286, 0.04


class _DemoDrone:
    def __init__(self, idx: int):
        self.drone_id = f"drone-{idx}"
        angle = 2 * math.pi * (idx - 1) / max(DRONE_COUNT, 1)
        self.lat = BCN_LAT + RADIUS * 0.6 * math.cos(angle)
        self.lon = BCN_LON + RADIUS * 0.6 * math.sin(angle)
        self.alt = random.uniform(50, 150)
        self.heading = random.uniform(0, 360)
        self.speed = random.uniform(5, 15)
        self.battery = random.uniform(60, 100)
        self.status = "patrolling"

    def step(self) -> dict:
        self.lat += random.gauss(0, 0.0005)
        self.lon += random.gauss(0, 0.0005)
        self.alt = max(20, self.alt + random.gauss(0, 2))
        self.heading = (self.heading + random.gauss(0, 10)) % 360
        self.speed = max(0, self.speed + random.gauss(0, 1))
        self.battery = max(0, self.battery - random.uniform(0.02, 0.1))
        if self.battery < 15:
            self.status = "returning"
        else:
            self.status = random.choices(["patrolling", "hovering"], weights=[0.85, 0.15])[0]

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
            "battery_pct": round(self.battery, 1),
            "status": self.status,
            "environment": {
                "temperature_c": round(random.uniform(5, 35), 1),
                "wind_speed_mps": round(random.uniform(0, 15), 1),
                "humidity_pct": round(random.uniform(30, 90), 1),
            },
        }


def _start_demo_generator():
    """Generate synthetic telemetry for demo/kiosk mode."""
    print(f"[Demo] Running in DEMO MODE — generating synthetic data for {DRONE_COUNT} drones")
    drones = [_DemoDrone(i) for i in range(1, DRONE_COUNT + 1)]
    while not _shutdown.is_set():
        for d in drones:
            payload = d.step()
            drone_state[d.drone_id] = payload
            socketio.emit("telemetry", payload)
        _shutdown.wait(3)


# ── Edge AI Analyzer ─────────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """You are a drone fleet analyst. Output ONLY a JSON object.
Format: {"fleet_status":"healthy","summary":"one sentence","insights":[{"type":"coverage","severity":"warning","drone_id":"drone-1","title":"Weak signal","detail":"RSRP -105dBm"}]}
Rules: 2 insights max, short titles (<8 words), short details (<15 words). RSRP<-100=poor, latency>15ms=high, battery<30%=critical, loss>1%=concerning."""


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
        rsrp = net.get("signal_rsrp_dbm", -80)
        sinr = net.get("signal_sinr_db", 20)
        lat_ms = net.get("latency_ms", 5)
        loss = net.get("packet_loss_pct", 0)
        dl = net.get("downlink_mbps", 0)
        lines.append(
            f"{did}: rsrp={rsrp}dBm sinr={sinr}dB lat={lat_ms}ms loss={loss}% dl={dl}Mbps bat={bat}%"
        )
    return "\n".join(lines)


def _try_parse_json(content: str):
    """Try to parse JSON, with truncation repair if needed."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # --- Repair truncated JSON ---
    repaired = content.rstrip()

    # 1. Close any unterminated string (odd number of unescaped quotes)
    #    Count quotes that aren't escaped
    in_string = False
    for i, ch in enumerate(repaired):
        if ch == '"' and (i == 0 or repaired[i - 1] != '\\'):
            in_string = not in_string
    if in_string:
        repaired += '"'

    # 2. Trim back to the last cleanly-ended value
    #    Remove trailing partial tokens after the last complete value
    while repaired and repaired[-1] in (',', ':', ' ', '\n', '\r', '\t'):
        repaired = repaired[:-1]

    # 3. Close open brackets/braces
    open_braces = repaired.count('{') - repaired.count('}')
    open_brackets = repaired.count('[') - repaired.count(']')
    repaired += ']' * max(0, open_brackets)
    repaired += '}' * max(0, open_braces)

    try:
        result = json.loads(repaired)
        print(f"[EdgeAI] JSON repaired successfully ({len(repaired)} chars)")
        return result
    except json.JSONDecodeError:
        pass

    # 4. More aggressive: trim back to last complete array element or object field
    #    Find last "}," or "]," or complete quoted string followed by comma/bracket
    for trim_to in ['"},', '],', '",', '}', ']', '"']:
        idx = content.rfind(trim_to)
        if idx > 0:
            candidate = content[:idx + len(trim_to)]
            # Remove trailing comma
            candidate = candidate.rstrip(',')
            # Close remaining structures
            ob = candidate.count('{') - candidate.count('}')
            oq = candidate.count('[') - candidate.count(']')
            candidate += ']' * max(0, oq)
            candidate += '}' * max(0, ob)
            try:
                result = json.loads(candidate)
                print(f"[EdgeAI] JSON repaired (aggressive, {len(candidate)} chars)")
                return result
            except json.JSONDecodeError:
                continue

    print("[EdgeAI] JSON repair failed")
    return None


def _normalize_ai_response(parsed) -> dict:
    """Normalize whatever JSON the model returned into our expected format.

    Phi-3 returns varying formats: string arrays, object arrays, nested dicts, etc.
    We transform any of these into the standard dashboard format.
    """
    import re as _re

    # If it's a list, transform array to standard format
    if isinstance(parsed, list):
        insights = []
        has_critical = False
        has_warning = False
        for item in parsed[:3]:
            if isinstance(item, str):
                # Insight is a plain string — extract info
                severity = "warning" if any(k in item.lower() for k in ["low", "weak", "poor", "high"]) else "info"
                if severity == "warning":
                    has_warning = True
                drone_match = _re.search(r'[Dd]rone[- ]?(\d+)', item)
                drone_id = f"drone-{drone_match.group(1)}" if drone_match else "fleet"
                insights.append({
                    "type": "performance",
                    "severity": severity,
                    "drone_id": drone_id,
                    "title": item[:60],
                    "detail": item[:120],
                })
            elif isinstance(item, dict):
                severity = str(item.get("severity", "warning")).lower()
                if "critical" in severity:
                    severity = "critical"
                    has_critical = True
                elif any(k in severity for k in ["warning", "medium", "high"]):
                    severity = "warning"
                    has_warning = True
                else:
                    severity = "info"
                insights.append({
                    "type": item.get("type", "performance"),
                    "severity": severity,
                    "drone_id": item.get("drone_id", item.get("drone", item.get("id", "fleet"))),
                    "title": str(item.get("title", item.get("issue", item.get("reason", "Issue"))))[:60],
                    "detail": str(item.get("detail", item.get("recommendation", item.get("reason", ""))))[:120],
                })
        status = "critical" if has_critical else ("degraded" if has_warning else "healthy")
        return {
            "fleet_status": status,
            "summary": f"AI detected {len(insights)} issue(s) across the fleet",
            "insights": insights,
        }

    # Must be a dict at this point
    if not isinstance(parsed, dict):
        return {"fleet_status": "healthy", "summary": "AI analysis complete", "insights": []}

    # Extract and normalize fleet_status
    fs = str(parsed.get("fleet_status", "healthy")).lower()
    if "critical" in fs or "danger" in fs:
        fs = "critical"
    elif any(k in fs for k in ["degrad", "maint", "warning", "attention"]):
        fs = "degraded"
    else:
        fs = "healthy"

    # Extract summary — model sometimes returns a dict of averages
    summary_raw = parsed.get("summary", "")
    if isinstance(summary_raw, dict):
        # Convert summary dict to a readable string
        parts = [f"{k}: {v}" for k, v in summary_raw.items()]
        summary = "Fleet averages — " + ", ".join(parts[:4])
    else:
        summary = str(summary_raw)[:200]

    # Extract and normalize insights
    insights_raw = parsed.get("insights", parsed.get("issues", parsed.get("results", [])))
    if isinstance(insights_raw, list):
        normalized = _normalize_ai_response(insights_raw)
        return {
            "fleet_status": fs,
            "summary": summary or normalized.get("summary", ""),
            "insights": normalized.get("insights", []),
        }

    return {"fleet_status": fs, "summary": summary, "insights": []}


def _parse_text_response(text: str) -> dict:
    """Parse a prose/text AI response into our standard insights format.

    When the model refuses to output JSON, we extract insights from its
    natural-language analysis by looking for drone IDs and issue keywords.
    """
    import re
    insights = []
    # Split by drone references
    drone_pattern = re.compile(r'[Dd]rone[- ]?(\d+)', re.IGNORECASE)
    severity_keywords = {
        "critical": ["critical", "severe", "danger", "very low", "extremely"],
        "warning": ["low", "weak", "high latency", "elevated", "poor", "concerning", "packet loss"],
        "info": ["normal", "good", "stable", "healthy", "optimal"],
    }
    type_keywords = {
        "coverage": ["rsrp", "signal", "coverage", "dbm", "sinr"],
        "performance": ["latency", "throughput", "bandwidth", "mbps", "speed", "downlink"],
        "battery": ["battery", "charge", "power"],
        "anomaly": ["loss", "packet", "error", "anomal"],
    }

    lines = text.split("\n")
    current_drone = None
    has_warning = False
    has_critical = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Check for drone ID
        dm = drone_pattern.search(line)
        if dm:
            current_drone = f"drone-{dm.group(1)}"

        # Check for issue indicators
        line_lower = line.lower()
        if any(kw in line_lower for kw in ["low", "weak", "high", "poor", "loss", "critical", "concern"]):
            # Determine severity
            severity = "info"
            for sev, keywords in severity_keywords.items():
                if any(kw in line_lower for kw in keywords):
                    severity = sev
                    break
            if severity == "critical":
                has_critical = True
            elif severity == "warning":
                has_warning = True

            # Determine type
            insight_type = "performance"
            for itype, keywords in type_keywords.items():
                if any(kw in line_lower for kw in keywords):
                    insight_type = itype
                    break

            # Clean up the line for display
            detail = re.sub(r'^[-*\d.)\s]+', '', line).strip()
            if len(detail) > 10 and current_drone and len(insights) < 3:
                insights.append({
                    "type": insight_type,
                    "severity": severity,
                    "drone_id": current_drone or "fleet",
                    "title": f"{insight_type.title()} issue" if len(detail) > 60 else detail[:60],
                    "detail": detail[:120],
                })

    status = "critical" if has_critical else ("degraded" if has_warning else "healthy")
    return {
        "fleet_status": status,
        "summary": f"AI analyzed fleet — {len(insights)} issue(s) detected",
        "insights": insights if insights else [{
            "type": "performance",
            "severity": "info",
            "drone_id": "fleet",
            "title": "Fleet analyzed",
            "detail": text[:120].strip(),
        }],
    }


def _call_edge_ai(prompt: str) -> dict | None:
    """Call Foundry Local via its OpenAI-compatible /v1/chat/completions API."""
    import urllib.request
    import urllib.error
    import ssl

    url = f"{EDGE_AI_ENDPOINT}/v1/chat/completions"
    body = json.dumps({
        "model": EDGE_AI_MODEL,
        "messages": [
            {"role": "system", "content": _AI_SYSTEM_PROMPT},
            {"role": "user", "content": f"5G drone fleet readings:\n{prompt}\nReturn ONLY a JSON object with fleet_status, summary, and insights array."},
        ],
        "temperature": 0.1,
        "max_tokens": 450,
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
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            result = json.loads(resp.read())
            # OpenAI-compatible response: choices[0].message.content
            choices = result.get("choices", [])
            if not choices:
                print("[EdgeAI] No choices in response")
                return None
            content = choices[0].get("message", {}).get("content", "")
            # Also check delta field (Foundry Local may return in delta)
            if not content:
                content = choices[0].get("delta", {}).get("content", "")
            print(f"[EdgeAI] Raw content ({len(content)} chars): {content[:300]}...")
            # Strip markdown fences if present
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            if content.startswith("json"):
                content = content[4:].strip()
            # Find the first JSON structure (object or array)
            obj_start = content.find("{")
            arr_start = content.find("[")
            if obj_start == -1 and arr_start == -1:
                # No JSON — parse the prose response into insights
                print("[EdgeAI] No JSON found, parsing text response")
                return _parse_text_response(content)
            # Use whichever comes first
            if arr_start != -1 and (obj_start == -1 or arr_start < obj_start):
                start = arr_start
            else:
                start = obj_start
            json_content = content[start:]
            # Remove trailing markdown fences or other non-JSON text after the structure
            import re
            json_content = re.sub(r'\s*```\s*$', '', json_content)
            json_content = json_content.strip()
            parsed = _try_parse_json(json_content)
            if parsed is None:
                # JSON extraction failed — fallback to text parsing
                print("[EdgeAI] JSON parse failed, parsing text response instead")
                return _parse_text_response(content)
            return _normalize_ai_response(parsed)
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
        print(f"[EdgeAI] API key: {EDGE_AI_API_KEY[:12]}...")

    # Wait for some telemetry data to accumulate
    while not _shutdown.is_set() and not drone_state:
        _shutdown.wait(2)

    while not _shutdown.is_set():
        try:
            if EDGE_AI_ENABLED:
                snapshot = _build_telemetry_snapshot()
                if snapshot:
                    result = _call_edge_ai(snapshot)
                    if result:
                        result["last_updated"] = datetime.now(timezone.utc).isoformat()
                        result["status"] = "connected"
                        result["model"] = EDGE_AI_MODEL
                        result["endpoint"] = EDGE_AI_ENDPOINT
                        ai_insights = result
                        socketio.emit("ai_insights", ai_insights)
                        print(f"[EdgeAI] Analysis complete — {result.get('fleet_status', '?')}, {len(result.get('insights', []))} insights")
                    else:
                        # Model call failed — fallback to rules
                        print("[EdgeAI] Model returned no result, falling back to rule engine")
                        result = _generate_demo_insights()
                        result["last_updated"] = datetime.now(timezone.utc).isoformat()
                        result["status"] = "fallback"
                        result["model"] = f"{EDGE_AI_MODEL} (fallback→rules)"
                        ai_insights = result
                        socketio.emit("ai_insights", ai_insights)
            else:
                # Demo mode — generate insights from rules
                result = _generate_demo_insights()
                result["last_updated"] = datetime.now(timezone.utc).isoformat()
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
    if DEMO_MODE:
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


if __name__ == "__main__":
    mode = "DEMO" if DEMO_MODE else "EVENT HUB"
    ai_mode = "EDGE AI" if EDGE_AI_ENABLED else "DEMO RULES"
    print(f"{'='*60}")
    print(f"  Drone Network Monitoring Dashboard — MWC 2026")
    print(f"  Telemetry : {mode}   Port: {PORT}   Drones: {DRONE_COUNT}")
    print(f"  Edge AI   : {ai_mode} → {EDGE_AI_ENDPOINT} ({EDGE_AI_MODEL})")
    print(f"{'='*60}")
    print()
    _start_background()
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
