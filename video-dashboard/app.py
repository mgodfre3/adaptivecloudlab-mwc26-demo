"""
Video Analysis Dashboard — Drone CV + Arc Video Indexer
=======================================================
Flask + Socket.IO server that:
  1. Accepts MP4 uploads from drone flights
  2. Runs CV inference for object detection (or generates demo data)
  3. Integrates with Arc Video Indexer for scene understanding
  4. Calls Foundry Local (Phi-4) for NL summaries of detections
  5. Pushes real-time processing updates via WebSocket

Env vars (set in video-dashboard/.env):
  DASHBOARD_PORT       – web server port (default 5000)
  EDGE_AI_ENDPOINT     – Foundry Local URL
  EDGE_AI_MODEL        – model name (default Phi-4-mini-instruct)
  EDGE_AI_API_KEY      – Foundry Local API key
  VI_ENDPOINT          – Arc Video Indexer API endpoint
  DATA_DIR             – upload / artifact storage (default /data)
  FLASK_SECRET_KEY     – Flask session secret
"""

import json
import os
import random
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_socketio import SocketIO

# ── Load config ──────────────────────────────────────────────────────────────
_here = Path(__file__).resolve().parent
_env_path = _here / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
EDGE_AI_ENDPOINT = os.getenv("EDGE_AI_ENDPOINT", "https://localhost:8443")
EDGE_AI_MODEL = os.getenv("EDGE_AI_MODEL", "Phi-4-mini-instruct")
EDGE_AI_API_KEY = os.getenv("EDGE_AI_API_KEY", "")
VI_ENDPOINT = os.getenv("VI_ENDPOINT", "http://video-indexer.video-indexer.svc:5000")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
FLASK_SECRET = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# ── Flask + Socket.IO ────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── In-memory video store ────────────────────────────────────────────────────
# video_id -> { id, filename, status, uploaded_at, detections, summary, ... }
videos: dict[str, dict] = {}
videos_lock = threading.Lock()

# ── Ensure data directories ─────────────────────────────────────────────────
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Demo-mode synthetic data
# ═════════════════════════════════════════════════════════════════════════════
DEMO_LABELS = [
    "antenna_panel", "antenna_panel", "antenna_panel",
    "cable_tray", "mounting_bracket", "weatherproofing",
    "rust_spot", "equipment_cabinet", "warning_sign",
    "lightning_rod", "obstruction_light",
]

DEMO_TOWER_LOCATIONS = [
    {"lat": 47.6423, "lng": -122.1301, "name": "Tower Alpha"},
    {"lat": 47.6435, "lng": -122.1285, "name": "Tower Bravo"},
    {"lat": 47.6410, "lng": -122.1320, "name": "Tower Charlie"},
]

DEMO_VIDEOS = [
    {
        "filename": "flight-alpha-north.mp4",
        "duration": 142.5,
        "tower": DEMO_TOWER_LOCATIONS[0],
        "detection_count": 18,
        "anomaly_rate": 0.11,
    },
    {
        "filename": "flight-bravo-sweep.mp4",
        "duration": 98.3,
        "tower": DEMO_TOWER_LOCATIONS[1],
        "detection_count": 12,
        "anomaly_rate": 0.0,
    },
    {
        "filename": "flight-charlie-closeup.mp4",
        "duration": 210.7,
        "tower": DEMO_TOWER_LOCATIONS[2],
        "detection_count": 27,
        "anomaly_rate": 0.22,
    },
    {
        "filename": "flight-alpha-thermal.mp4",
        "duration": 65.0,
        "tower": DEMO_TOWER_LOCATIONS[0],
        "detection_count": 9,
        "anomaly_rate": 0.33,
    },
]


def _generate_detections(video_cfg: dict) -> list[dict]:
    """Generate realistic synthetic detection data for demo mode."""
    detections = []
    duration = video_cfg["duration"]
    fps = 30.0
    total_frames = int(duration * fps)
    n = video_cfg["detection_count"]

    for i in range(n):
        frame = random.randint(1, total_frames)
        timestamp = round(frame / fps, 2)
        label = random.choice(DEMO_LABELS)
        is_anomaly = label == "rust_spot" or (
            random.random() < video_cfg["anomaly_rate"] and label not in ("antenna_panel",)
        )
        confidence = round(random.uniform(0.72, 0.98) if not is_anomaly else random.uniform(0.55, 0.82), 2)

        # Bounding box (normalised 0-1)
        cx, cy = random.uniform(0.2, 0.8), random.uniform(0.15, 0.85)
        w, h = random.uniform(0.05, 0.2), random.uniform(0.05, 0.25)
        bbox = {
            "x": round(max(0, cx - w / 2), 3),
            "y": round(max(0, cy - h / 2), 3),
            "w": round(w, 3),
            "h": round(h, 3),
        }

        tower = video_cfg["tower"]
        detections.append({
            "id": f"det-{i:04d}",
            "frame": frame,
            "timestamp": timestamp,
            "label": label,
            "confidence": confidence,
            "is_anomaly": is_anomaly,
            "bbox": bbox,
            "geo": {
                "lat": round(tower["lat"] + random.uniform(-0.0005, 0.0005), 6),
                "lng": round(tower["lng"] + random.uniform(-0.0005, 0.0005), 6),
                "alt_m": round(random.uniform(15, 45), 1),
            },
        })

    detections.sort(key=lambda d: d["frame"])
    return detections


def _generate_summary(video_cfg: dict, detections: list[dict]) -> dict:
    """Generate a synthetic analysis summary."""
    labels = {}
    anomalies = 0
    for d in detections:
        labels[d["label"]] = labels.get(d["label"], 0) + 1
        if d["is_anomaly"]:
            anomalies += 1

    avg_conf = round(sum(d["confidence"] for d in detections) / max(len(detections), 1), 2)
    tower_name = video_cfg["tower"]["name"]

    if anomalies == 0:
        condition = "All structures appear intact with no anomalies detected."
        severity = "nominal"
    elif anomalies <= 2:
        condition = f"{anomalies} minor anomaly detected — recommend follow-up inspection."
        severity = "advisory"
    else:
        condition = f"{anomalies} anomalies detected including potential corrosion — maintenance recommended."
        severity = "warning"

    return {
        "tower": tower_name,
        "total_detections": len(detections),
        "unique_labels": len(labels),
        "label_counts": labels,
        "anomaly_count": anomalies,
        "avg_confidence": avg_conf,
        "severity": severity,
        "ai_summary": (
            f"Analysis of {tower_name}: {len(detections)} objects detected across "
            f"{round(video_cfg['duration'])}s of drone footage. "
            f"Identified {labels.get('antenna_panel', 0)} antenna panels, "
            f"{labels.get('cable_tray', 0)} cable trays, and "
            f"{labels.get('equipment_cabinet', 0)} equipment cabinets. "
            f"{condition} Average detection confidence: {avg_conf*100:.0f}%."
        ),
        "vi_insights": {
            "scenes": random.randint(3, 8),
            "keyframes_extracted": random.randint(12, 40),
            "dominant_colors": ["#4a5568", "#2d3748", "#718096"],
            "motion_segments": random.randint(2, 6),
        },
    }


def _seed_demo_videos():
    """Populate the store with pre-analysed demo videos."""
    for cfg in DEMO_VIDEOS:
        vid_id = str(uuid.uuid4())[:8]
        detections = _generate_detections(cfg)
        summary = _generate_summary(cfg, detections)
        with videos_lock:
            videos[vid_id] = {
                "id": vid_id,
                "filename": cfg["filename"],
                "status": "complete",
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "duration": cfg["duration"],
                "detections": detections,
                "summary": summary,
                "pipeline": {
                    "upload": "complete",
                    "cv_inference": "complete",
                    "vi_indexing": "complete",
                    "ai_summary": "complete",
                },
            }


# ═════════════════════════════════════════════════════════════════════════════
#  AI / inference helpers
# ═════════════════════════════════════════════════════════════════════════════

def _call_foundry_local(prompt: str, system: str | None = None) -> str | None:
    """Call Foundry Local chat completions endpoint."""
    if not EDGE_AI_ENDPOINT or not EDGE_AI_API_KEY:
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        with httpx.Client(verify=False, timeout=30.0) as client:
            resp = client.post(
                f"{EDGE_AI_ENDPOINT.rstrip('/')}/v1/chat/completions",
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {EDGE_AI_API_KEY}",
                },
                json={
                    "model": EDGE_AI_MODEL,
                    "messages": messages,
                    "max_tokens": 512,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as exc:
        app.logger.warning("Foundry Local call failed: %s", exc)
        return None


def _check_foundry_local() -> bool:
    """Health-check the Foundry Local endpoint."""
    if not EDGE_AI_ENDPOINT:
        return False
    try:
        with httpx.Client(verify=False, timeout=5.0) as client:
            resp = client.get(f"{EDGE_AI_ENDPOINT.rstrip('/')}/v1/models",
                              headers={"Authorization": f"Bearer {EDGE_AI_API_KEY}"})
            return resp.status_code == 200
    except Exception:
        return False


def _check_video_indexer() -> bool:
    """Health-check the Arc Video Indexer endpoint."""
    if not VI_ENDPOINT:
        return False
    try:
        with httpx.Client(verify=False, timeout=5.0) as client:
            resp = client.get(f"{VI_ENDPOINT.rstrip('/')}/health")
            return resp.status_code == 200
    except Exception:
        return False


SYSTEM_PROMPT = (
    "You are a drone video analyst working at a cell-tower inspection site. "
    "Given detection data from a drone flight analyzing cell tower antennas, "
    "provide a concise, professional summary of findings. Focus on structural "
    "integrity, anomalies, and maintenance recommendations."
)


# ═════════════════════════════════════════════════════════════════════════════
#  Processing pipeline (background thread per upload)
# ═════════════════════════════════════════════════════════════════════════════

def _process_video(video_id: str):
    """Run the full analysis pipeline for an uploaded video."""
    with videos_lock:
        vid = videos.get(video_id)
    if not vid:
        return

    # ── Step 1: CV Inference ─────────────────────────────────────────────
    _update_pipeline(video_id, "cv_inference", "running")
    socketio.emit("processing_update", {"id": video_id, "step": "cv_inference", "status": "running"})

    # Simulate inference time (2-4 seconds for demo)
    time.sleep(random.uniform(2.0, 4.0))

    # Generate demo detections
    cfg = {
        "filename": vid["filename"],
        "duration": vid.get("duration") or random.uniform(60, 240),
        "tower": random.choice(DEMO_TOWER_LOCATIONS),
        "detection_count": random.randint(8, 30),
        "anomaly_rate": random.uniform(0.0, 0.25),
    }
    detections = _generate_detections(cfg)

    with videos_lock:
        videos[video_id]["detections"] = detections
        videos[video_id]["duration"] = cfg["duration"]

    _update_pipeline(video_id, "cv_inference", "complete")
    socketio.emit("detection_complete", {
        "id": video_id,
        "detection_count": len(detections),
        "step": "cv_inference",
        "status": "complete",
    })

    # ── Step 2: Video Indexer ────────────────────────────────────────────
    _update_pipeline(video_id, "vi_indexing", "running")
    socketio.emit("processing_update", {"id": video_id, "step": "vi_indexing", "status": "running"})

    time.sleep(random.uniform(1.5, 3.0))

    _update_pipeline(video_id, "vi_indexing", "complete")
    socketio.emit("indexing_complete", {"id": video_id, "step": "vi_indexing", "status": "complete"})

    # ── Step 3: AI Summary ───────────────────────────────────────────────
    _update_pipeline(video_id, "ai_summary", "running")
    socketio.emit("processing_update", {"id": video_id, "step": "ai_summary", "status": "running"})

    summary = _generate_summary(cfg, detections)

    # Try Foundry Local for a real AI summary
    detection_text = json.dumps(
        [{"label": d["label"], "confidence": d["confidence"],
          "is_anomaly": d["is_anomaly"], "timestamp": d["timestamp"]} for d in detections],
        indent=2,
    )
    ai_response = _call_foundry_local(
        f"Analyze these drone flight detections and provide a summary:\n{detection_text}",
        system=SYSTEM_PROMPT,
    )
    if ai_response:
        summary["ai_summary"] = ai_response
        summary["ai_source"] = "foundry-local"
    else:
        summary["ai_source"] = "demo-generated"

    time.sleep(random.uniform(0.5, 1.5))

    with videos_lock:
        videos[video_id]["summary"] = summary
        videos[video_id]["status"] = "complete"

    _update_pipeline(video_id, "ai_summary", "complete")
    socketio.emit("analysis_complete", {
        "id": video_id,
        "summary": summary,
        "step": "ai_summary",
        "status": "complete",
    })


def _update_pipeline(video_id: str, step: str, status: str):
    with videos_lock:
        if video_id in videos:
            videos[video_id]["pipeline"][step] = status


# ═════════════════════════════════════════════════════════════════════════════
#  Routes
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload_video():
    """Receive MP4 file upload and start processing pipeline."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Only MP4 files are accepted"}), 400

    video_id = str(uuid.uuid4())[:8]
    safe_name = f"{video_id}_{f.filename.replace(' ', '_')}"
    dest = UPLOAD_DIR / safe_name
    f.save(str(dest))

    video_url = f"/data/uploads/{safe_name}"

    with videos_lock:
        videos[video_id] = {
            "id": video_id,
            "filename": f.filename,
            "video_url": video_url,
            "status": "processing",
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "duration": None,
            "detections": [],
            "summary": None,
            "pipeline": {
                "upload": "complete",
                "cv_inference": "pending",
                "vi_indexing": "pending",
                "ai_summary": "pending",
            },
        }

    socketio.emit("processing_started", {"id": video_id, "filename": f.filename, "video_url": video_url})

    thread = threading.Thread(target=_process_video, args=(video_id,), daemon=True)
    thread.start()

    return jsonify({"id": video_id, "status": "processing"}), 202


@app.route("/api/videos")
def list_videos():
    """List all uploaded/processed videos with status."""
    with videos_lock:
        result = []
        for v in videos.values():
            result.append({
                "id": v["id"],
                "filename": v["filename"],
                "video_url": v.get("video_url"),
                "status": v["status"],
                "uploaded_at": v["uploaded_at"],
                "duration": v.get("duration"),
                "detection_count": len(v.get("detections", [])),
                "pipeline": v.get("pipeline", {}),
            })
    return jsonify(result)


@app.route("/api/videos/<video_id>/detections")
def get_detections(video_id: str):
    """Get detection results JSON for a video."""
    with videos_lock:
        vid = videos.get(video_id)
    if not vid:
        return jsonify({"error": "Video not found"}), 404
    return jsonify(vid.get("detections", []))


@app.route("/api/videos/<video_id>/summary")
def get_summary(video_id: str):
    """Get summary stats for a video."""
    with videos_lock:
        vid = videos.get(video_id)
    if not vid:
        return jsonify({"error": "Video not found"}), 404
    return jsonify(vid.get("summary") or {"status": "processing"})


@app.route("/api/videos/<video_id>/geojson")
def get_geojson(video_id: str):
    """Export detections as GeoJSON FeatureCollection."""
    with videos_lock:
        vid = videos.get(video_id)
    if not vid:
        return jsonify({"error": "Video not found"}), 404

    features = []
    for d in vid.get("detections", []):
        geo = d.get("geo", {})
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [geo.get("lng", 0), geo.get("lat", 0), geo.get("alt_m", 0)],
            },
            "properties": {
                "id": d["id"],
                "label": d["label"],
                "confidence": d["confidence"],
                "frame": d["frame"],
                "timestamp": d["timestamp"],
                "is_anomaly": d["is_anomaly"],
            },
        })

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
    })


@app.route("/api/videos/<video_id>/query", methods=["POST"])
def query_video(video_id: str):
    """Send NL query to Foundry Local about this video's detections."""
    with videos_lock:
        vid = videos.get(video_id)
    if not vid:
        return jsonify({"error": "Video not found"}), 404

    body = request.get_json(silent=True) or {}
    question = body.get("query", "").strip()
    if not question:
        return jsonify({"error": "No query provided"}), 400

    detections = vid.get("detections", [])
    summary = vid.get("summary", {})

    context = (
        f"Video: {vid['filename']}\n"
        f"Summary: {summary.get('ai_summary', 'N/A')}\n"
        f"Detections ({len(detections)} total):\n"
        + json.dumps(
            [{"label": d["label"], "confidence": d["confidence"],
              "is_anomaly": d["is_anomaly"], "timestamp": d["timestamp"]}
             for d in detections[:50]],  # limit context size
            indent=2,
        )
    )

    ai_response = _call_foundry_local(
        f"Based on this drone video analysis data:\n{context}\n\nQuestion: {question}",
        system=SYSTEM_PROMPT,
    )

    if ai_response:
        return jsonify({"answer": ai_response, "source": "foundry-local"})

    # Demo fallback
    fallback = _demo_query_response(question, vid)
    return jsonify({"answer": fallback, "source": "demo-generated"})


def _demo_query_response(question: str, vid: dict) -> str:
    """Generate a plausible demo answer when Foundry Local is unavailable."""
    detections = vid.get("detections", [])
    summary = vid.get("summary", {})
    q = question.lower()

    if "antenna" in q:
        count = summary.get("label_counts", {}).get("antenna_panel", 0)
        return (f"Detected {count} antenna panels across the flight. "
                f"All panels appear properly mounted with an average detection "
                f"confidence of {summary.get('avg_confidence', 0.85)*100:.0f}%.")
    elif "anomal" in q or "damage" in q or "rust" in q:
        anom = summary.get("anomaly_count", 0)
        if anom == 0:
            return "No anomalies were detected during this flight. All inspected structures appear to be in good condition."
        return (f"{anom} potential anomalies were flagged, including possible "
                f"corrosion or weathering damage. A closer physical inspection "
                f"is recommended for the flagged areas.")
    elif "summary" in q or "overview" in q:
        return summary.get("ai_summary", "Analysis is still processing.")
    else:
        return (f"Based on the analysis of {vid['filename']}: "
                f"{len(detections)} objects were detected with an average "
                f"confidence of {summary.get('avg_confidence', 0.85)*100:.0f}%. "
                f"The inspection covers {summary.get('tower', 'the target area')}.")


@app.route("/api/ai-status")
def ai_status():
    """Health check for Foundry Local + Video Indexer."""
    fl_ok = _check_foundry_local()
    vi_ok = _check_video_indexer()
    return jsonify({
        "foundry_local": {"status": "healthy" if fl_ok else "unavailable", "endpoint": EDGE_AI_ENDPOINT},
        "video_indexer": {"status": "healthy" if vi_ok else "unavailable", "endpoint": VI_ENDPOINT},
        "demo_mode": not fl_ok,
    })


# ── Serve uploaded files for playback ────────────────────────────────────────
@app.route("/data/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(str(UPLOAD_DIR), filename)


# ═════════════════════════════════════════════════════════════════════════════
#  Socket.IO events
# ═════════════════════════════════════════════════════════════════════════════

@socketio.on("connect")
def handle_connect():
    app.logger.info("Client connected")
    with videos_lock:
        video_list = [
            {"id": v["id"], "filename": v["filename"], "video_url": v.get("video_url"),
             "status": v["status"], "pipeline": v.get("pipeline", {})}
            for v in videos.values()
        ]
    socketio.emit("video_list", video_list)


# ═════════════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _seed_demo_videos()
    app.logger.info("Video Analysis Dashboard starting on port %d", PORT)
    app.logger.info("Demo videos seeded: %d", len(videos))
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False, allow_unsafe_werkzeug=True)
