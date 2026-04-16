# Drone Video Intelligence Demo Guide

**Adaptive Cloud Lab — Video Indexer + Foundry Local on Azure Local**

> **Headline:** *"Upload drone footage, detect antennas with custom AI, search and query results with natural language — all running on edge hardware, powered by Foundry Local."*

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The Video Intelligence Dashboard](#the-video-intelligence-dashboard)
3. [CV Inference Pipeline — YOLOv8 on the Edge](#cv-inference-pipeline)
4. [Arc Video Indexer — Local Video Intelligence](#arc-video-indexer)
5. [Foundry Local — The Star of the Demo](#foundry-local)
6. [Cloud + Edge: Model Training Story](#cloud--edge-model-training-story)
7. [Demo Flow Checklist](#demo-flow-checklist)

---

## Architecture Overview

**Talk track (Minute 1–2)**

This demo runs on a dedicated **AKS Arc cluster** on the **Mobile Azure Local stamp** — completely separate from the drone telemetry dashboard. Two edge demos, two clusters, one platform.

### Cluster Layout

| Node Role | Count | Spec | Purpose |
|---|---|---|---|
| System Nodes | 2 | Standard_D4s_v3 | NGINX Ingress, cert-manager, MetalLB |
| User Nodes | 2 | Standard_D4s_v3 | Video Dashboard, CV inference jobs |
| GPU Node | 1 | Standard_NC4_A2 (NVIDIA A2, 16 GB) | Foundry Local + YOLOv8 + Video Indexer |

```
Mobile Azure Local Stamp (AD-less, DNS: acx.mobile)
└── AKS Arc Cluster (vi-aks)
    ├── System Nodes           ← Platform services
    ├── User Nodes             ← Video Dashboard + processing
    └── GPU Node (NVIDIA A2)   ← Foundry Local + YOLO + VI
```

### Network Details

| Component | Value |
|---|---|
| Dashboard URL | `https://video.acx.mobile` |
| Grafana URL | `https://grafana-vi.acx.mobile` |
| DNS Zone | `acx.mobile` (AD-less design) |
| Foundry Endpoint (in-cluster) | `https://phi-4-deployment.vi-foundry-mdl.svc:5000` |

---

## The Video Intelligence Dashboard

**Talk track (Minute 2–4)**

### What Visitors See

A sleek, dark-themed web UI where booth visitors can:

1. **Upload drone footage** — Drag-and-drop or browse for MP4 files
2. **Watch AI process the video** — Real-time progress: Upload → CV Detection → Video Indexing → AI Summary
3. **View detections** — Video player with bounding box overlays at antenna locations
4. **Browse the detection timeline** — Horizontal bar showing where in the video antennas were found
5. **Read the AI summary** — Foundry Local (Phi-4 Mini) provides a natural language summary of findings
6. **Ask questions** — Type natural language queries: "How many antennas were detected?" "Where are they located?"
7. **Export results** — Download detection data as GeoJSON, CSV, or JSON

### Processing Pipeline (What Happens Behind the Scenes)

```
MP4 Upload → CV Inference (YOLOv8 on GPU) → Arc Video Indexer → Foundry Local Summary
   ~10s           ~15-30s                      ~60-120s              ~3-5s
```

Each step emits real-time progress updates via WebSocket — visitors see the pipeline executing live.

---

## CV Inference Pipeline

**Talk track (Minute 4–5)**

### The Model

**YOLOv8s** fine-tuned for cellular antenna detection:

| Attribute | Value |
|---|---|
| Base model | YOLOv8s (Ultralytics) |
| Fine-tuning dataset | Roboflow RF100 Cell Towers + custom drone frames |
| Classes | 1 (`cellular_antenna`) |
| Input size | 640×640 |
| Format | ONNX (exported from PyTorch) |
| Inference runtime | ONNX Runtime with CUDA on NVIDIA A2 |
| Speed | ~5-10 ms/frame (100-200 FPS) |
| VRAM usage | ~1 GB |

### Key talking point

> "This model was **trained in Azure** using cloud GPU compute, then **exported as ONNX and deployed at the edge**. That's the hybrid cloud story — use the cloud for what it's good at (training), run inference where the data lives (the edge)."

### Per-Frame Output

For each detection, the pipeline generates structured metadata:

```json
{
  "frame": 1234,
  "timestamp_str": "00:00:41.133",
  "objects": [{
    "label": "cellular_antenna",
    "confidence": 0.91,
    "bbox_xyxy": [412, 128, 487, 256]
  }]
}
```

This converts raw video pixels into **searchable, queryable events**.

---

## Arc Video Indexer

**Talk track (Minute 5–6)**

### What It Does

Azure AI Video Indexer **enabled by Arc** runs as a Kubernetes extension directly on the cluster. It provides:

- **Timeline events** — Scrub to any moment where an antenna was detected
- **Custom labels** — "cellular_antenna" labels registered from our YOLO pipeline
- **Built-in AI** — Transcription, scene detection, OCR on top of our custom detections
- **Search** — Filter video segments by label, time range, confidence

### Integration Pattern: Metadata-Driven Indexing

We don't rely on Video Indexer's built-in vision for antenna detection (it doesn't know what antennas look like). Instead:

1. Our YOLO pipeline runs first → produces structured detection JSON
2. We feed **video + sidecar metadata** to Video Indexer
3. Video Indexer creates a searchable index with our custom labels + its own insights

**Key talking point:**
> "Video Indexer handles the heavy lifting of video intelligence — transcription, scene detection, search. We augment it with our own custom vision model for domain-specific objects like cell tower antennas. Best of both worlds."

---

## Foundry Local — The Star of the Demo

**Talk track (Minute 6–7)**

### Why Foundry Local Matters

Foundry Local is the **orchestration and intelligence layer** that ties everything together:

| Capability | What It Does |
|---|---|
| **NL Summarization** | Phi-4 Mini reads detection metadata and produces executive-level summaries |
| **Interactive Queries** | Visitors type natural language questions about the video analysis |
| **Cross-Video Correlation** | Compare detections across multiple flights |
| **Report Generation** | Generate structured findings from raw detection data |

### The Foundry Local Operator

Deployed via 61 lines of YAML using the **Foundry Local Inference Operator** (Private Preview):

```yaml
apiVersion: foundrylocal.azure.com/v1
kind: ModelDeployment
metadata:
  name: phi-4-deployment
spec:
  model:
    ref: phi-4-mini
  workloadType: generative
  compute: gpu
  replicas: 1
```

The operator automatically:
- Downloads Phi-4 Mini from the Foundry catalog
- Schedules on the GPU node
- Creates TLS certificates
- Generates API keys
- Exposes an **OpenAI-compatible API** (`/v1/chat/completions`)

### Sample Interaction

**Visitor asks:** "What did the drone find?"

**Foundry Local responds:** "The drone flight detected 3 cellular antenna panels across 2 tower structures. All detections occurred between timestamps 0:41 and 2:15, at altitudes of 45-60 meters. Average detection confidence was 87%. The antennas appear to be standard macro cell panels oriented in a tri-sector configuration."

**Key talking point:**
> "Foundry Local gives you an OpenAI-compatible API running on a $300 GPU card at the edge. No cloud API calls, no per-token billing, no data leaving the premises. This is enterprise AI at the edge."

---

## Cloud + Edge: Model Training Story

**Talk track (when asked about the model)**

This is a deliberate architectural choice worth highlighting:

| Phase | Where | Why |
|---|---|---|
| **Data labeling** | Azure (Roboflow / Custom Vision) | Cloud tools are better for collaborative labeling |
| **Model training** | Azure ML (cloud GPU) | Training needs lots of compute, but only once |
| **Model export** | Azure → ONNX | Open format, runs anywhere |
| **Inference** | Azure Local (edge GPU) | Where the data lives, low latency, data sovereignty |

> "We used the cloud for what it's best at — scalable compute for training. Then we brought the trained model to the edge where the data lives. No raw video ever leaves these servers."

This is a key differentiator from "cloud-only" or "edge-only" approaches.

---

## Demo Flow Checklist

### Pre-Demo (Day Before)

- [ ] Cluster deployed: `kubectl get nodes` shows 5+ nodes
- [ ] Foundry Local running: `kubectl get pods -n vi-foundry-mdl`
- [ ] Video Indexer extension healthy: `az k8s-extension show --name videoindexer ...`
- [ ] Dashboard accessible at `https://video.acx.mobile`
- [ ] Grafana accessible at `https://grafana-vi.acx.mobile`
- [ ] Pre-upload 2-3 sample drone videos for instant demo
- [ ] Verify GPU metrics in Grafana
- [ ] Test NL query in dashboard

### 8-Minute Demo Flow

| Time | Show | Say |
|---|---|---|
| **0–2 min** | Architecture diagram + cluster overview | *"Dedicated AKS Arc cluster on Azure Local. GPU running Foundry Local for edge AI."* |
| **2–4 min** | Upload a drone video, watch processing | *"Drag and drop a drone flight. Custom YOLO model detects antennas on the GPU. Video Indexer creates a searchable timeline."* |
| **4–5 min** | Detection results + video player | *"Every antenna detection has a confidence score, bounding box, and timestamp. Click any detection to jump to that moment."* |
| **5–7 min** | Foundry Local NL query | *"Ask Foundry Local: 'Summarize what the drone found.' Phi-4 Mini analyzes the detections and gives you an executive summary — running on a $300 GPU card."* |
| **7–8 min** | Export + data sovereignty message | *"Export as GeoJSON for your GIS team. All processing happened on these two servers. No video left the premises."* |

### Closing Statement

> *"This demo shows the full AI lifecycle on Azure Local: models trained in the cloud, deployed at the edge with Foundry Local, augmented by Arc Video Indexer, all running on commodity hardware with full data sovereignty."*

### Fallback Options

| Issue | Fallback |
|---|---|
| Dashboard not responding | Demo mode generates synthetic results; restart pod |
| GPU unavailable | Dashboard falls back to demo mode with pre-computed results |
| Video Indexer not ready | Skip VI step; show YOLO detections + Foundry summary only |
| Upload fails | Use pre-uploaded sample videos |
| Network issues | Everything runs locally; DNS may need hosts file entry |

### Compelling Query Scenarios

Try these during the demo:
- "Show me all video segments where an antenna was detected"
- "How many antennas were found in this flight?"
- "What's the average confidence of the detections?"
- "Were any antennas detected at high altitude?"
- "Compare this flight to the previous one"

---

## Technical Reference

### Namespaces

| Namespace | Contents |
|---|---|
| `video-analysis` | Dashboard + CV inference jobs |
| `vi-foundry-mdl` | Foundry Local Phi-4 deployment |
| `vi-foundry-op` | Foundry Local operator |
| `longhorn-system` | Longhorn storage (RWX for Video Indexer) |
| `monitoring` | Prometheus + Grafana + DCGM |
| `vi-ingress` | NGINX Ingress Controller |
| `cert-manager` | TLS certificate management |

### Key URLs

| Service | URL |
|---|---|
| Video Dashboard | `https://video.acx.mobile` |
| Grafana | `https://grafana-vi.acx.mobile` |
| Foundry Local API (in-cluster) | `https://phi-4-deployment.vi-foundry-mdl.svc:5000` |
| Video Indexer API (in-cluster) | Via extension endpoint |

### GPU Budget (NVIDIA A2, 16 GB VRAM)

| Workload | VRAM | Duration |
|---|---|---|
| Foundry Local (Phi-4 Mini) | ~8 GB | Always-on |
| YOLOv8s ONNX inference | ~1 GB | Per-video job (~30s) |
| Arc Video Indexer | ~4-6 GB | During indexing |
| **Headroom** | ~1-3 GB | Safety margin |

GPU workloads are time-sliced: YOLO runs as short batch jobs, Foundry stays loaded, VI uses GPU during indexing windows.
