# Deep Dive: Infrastructure, Data Flow, and Edge AI Insights

**MWC 2026 Demo — Adaptive Cloud Lab**

This document provides a thorough examination of how the Real-Time Drone Network Monitoring demo works, including the infrastructure, the complete data flow, how AI Insights runs locally on-premises, and what data is collected and where it goes.

---

## Table of Contents

1. [Infrastructure Overview](#1-infrastructure-overview)
2. [Data Collection: What Is Being Measured](#2-data-collection-what-is-being-measured)
3. [Data Flow: End-to-End Walkthrough](#3-data-flow-end-to-end-walkthrough)
4. [AI Insights: Running Locally at the Edge](#4-ai-insights-running-locally-at-the-edge)
5. [Data Privacy and On-Premises Boundaries](#5-data-privacy-and-on-premises-boundaries)
6. [Edge Computing: Why This Runs at the Edge](#6-edge-computing-why-this-runs-at-the-edge)
7. [Component Reference](#7-component-reference)

---

## 1. Infrastructure Overview

The entire demo runs on two physical servers in the exhibit space, managed as a Kubernetes cluster through Azure Arc — no cloud compute is required.

### Physical Hardware

| Item | Spec |
|---|---|
| Servers | 2× Lenovo ThinkEdge SE350 |
| RAM | 128 GB per node (256 GB total) |
| GPU | 1× NVIDIA A2 per node (16 GB VRAM, Ampere architecture) |
| OS | Azure Linux 3.0 |
| Role | Azure Local (HCI) cluster nodes |

### Kubernetes Cluster

The two physical servers host a 6-node AKS Arc cluster (AKS deployed on Azure Local):

```
Physical Servers (Azure Local HCI)
└── AKS Arc Cluster
    ├── Control Plane Node    (moc-l4kkzr6yfnr  172.21.229.195)
    ├── System Node 1         (moc-lk28q8q9ct9  172.21.229.196)  ← platform services
    ├── System Node 2         (moc-lujmurkd13i  172.21.229.197)  ← platform services
    ├── User Node 1           (pdxuser pool)                      ← dashboard + simulator
    ├── User Node 2           (pdxuser pool)                      ← dashboard + simulator
    └── GPU Node              (pdxgpu pool, NVIDIA A2)            ← Phi-4 AI inference
```

The VMs run inside the Lenovo SE350 hypervisor managed by Azure Local. Azure Arc connects the cluster to Azure for management (RBAC, policy, GitOps) without routing any workload traffic through Azure.

### Platform Services (System Nodes)

| Service | Namespace | Role |
|---|---|---|
| **MetalLB v0.14.9** | `metallb-system` | L2 load balancer — assigns VIP `172.21.229.201` to ingress |
| **NGINX Ingress** | `pdx-ingress` | TLS termination, routes external HTTPS to dashboard |
| **cert-manager v1.19.2** | `cert-manager` | Issues self-signed TLS certificates |
| **trust-manager v0.20.3** | `cert-manager` | Distributes CA trust bundles across namespaces |
| **Azure IoT Operations** | `azure-iot-operations` | MQ broker + data pipeline (future expansion) |
| **Prometheus + Grafana** | `monitoring` | Cluster metrics, GPU metrics via DCGM Exporter |

### Workload Namespaces

```
drone-demo/
├── dashboard (Flask + Socket.IO + Leaflet.js)  — reads IoT Hub, serves UI, calls AI
└── simulator (Python)                          — optional; sends telemetry to IoT Hub

foundry-local/
├── Foundry Local Inference Operator            — manages SLM lifecycle on GPU
└── phi-4-deployment (Phi-4 Mini)               — AI inference endpoint :5000
```

### Azure Cloud (South Central US)

Only the following Azure resources are used — no Azure compute or networking is involved in serving the demo:

| Resource | Purpose |
|---|---|
| **Azure IoT Hub** (`pdx-iothub`, S1) | Device registry and D2C telemetry ingestion |
| **Azure Key Vault** (`pdx-kv`) | Stores drone connection strings (used only at startup) |
| **Azure Container Registry** (`acxcontregwus2`) | Stores dashboard and simulator container images |
| **Azure Arc Connected Cluster** | Management plane for the on-premises Kubernetes cluster |

---

## 2. Data Collection: What Is Being Measured

### Telemetry Payload Structure

Every drone sends (or the dashboard generates in demo mode) one telemetry message every 3–5 seconds. Each message is a JSON object with the following fields:

```json
{
  "drone_id": "drone-alpha",
  "timestamp": "2026-02-28T15:00:00.000Z",

  "location": {
    "latitude": 41.3650,
    "longitude": 2.1500,
    "altitude_m": 120.0,
    "heading_deg": 45.0,
    "speed_mps": 12.5
  },

  "network": {
    "signal_rsrp_dbm": -85,       // Reference Signal Received Power
    "signal_rsrq_db": -10.5,      // Reference Signal Received Quality
    "signal_sinr_db": 18.2,       // Signal-to-Interference+Noise Ratio
    "cell_id": 523841,            // Serving cell identifier
    "band": "n78",                // 5G NR frequency band
    "downlink_mbps": 650.0,       // Downlink throughput
    "uplink_mbps": 85.0,          // Uplink throughput
    "latency_ms": 4.2,            // Round-trip latency
    "packet_loss_pct": 0.012,     // Packet loss percentage
    "connected": true             // 5G connection status
  },

  "battery_pct": 72.5,            // Battery level
  "status": "patrolling",         // Drone lifecycle state

  "environment": {
    "temperature_c": 18.0,
    "wind_speed_mps": 3.5,
    "humidity_pct": 65.0
  }
}
```

### 5G Network Metrics Explained

The demo simulates the kind of measurements a drone-mounted 5G UE (user equipment) would report when acting as a flying network quality probe:

| Metric | What It Measures | Range | Threshold |
|---|---|---|---|
| **RSRP** (dBm) | Signal strength from the serving cell | -140 to -44 | Good: > -80; Poor: < -100 |
| **RSRQ** (dB) | Signal quality relative to interference | -20 to -3 | Good: > -10; Poor: < -15 |
| **SINR** (dB) | Useful signal vs. noise + interference | -20 to 30 | Good: > 10; Poor: < 0 |
| **DL Throughput** (Mbps) | Downlink data rate | 50–1200 | Excellent: > 500 |
| **UL Throughput** (Mbps) | Uplink data rate | 10–200 | — |
| **Latency** (ms) | Round-trip time | 1–25 | Alert: > 15 ms |
| **Packet Loss** (%) | Lost packets | 0–2% | Alert: > 1% |

### Drone Lifecycle States

Each drone follows a state machine. The `status` field in the telemetry reflects the current state:

```
launching → patrolling ⇄ hovering → returning → landing → charging → (new drone)
```

| State | Description |
|---|---|
| `launching` | Climbing from base pad to patrol altitude (8 ticks / ~24 s) |
| `patrolling` | Flying between Barcelona waypoints at 8–18 m/s |
| `hovering` | Brief pause at a waypoint (3–8 ticks / 9–24 s) |
| `returning` | Battery ≤ 18% — flying home at max speed |
| `landing` | Descending at base (6 ticks / ~18 s) |
| `charging` | On the ground, recharging at 0.35%/tick until 92% |

When a drone reaches 92% charge, it is **retired** and replaced by a new drone with the next NATO callsign (Alpha → Bravo → … → Zulu → Alpha, cycling indefinitely).

---

## 3. Data Flow: End-to-End Walkthrough

### Mode 1: Live IoT Hub Mode

```
[Drone Simulator Pod]
        │
        │  1. AMQP over TLS (port 5671)
        │     Message: JSON telemetry payload
        │     Custom property: drone_id
        ▼
[Azure IoT Hub — pdx-iothub]
        │
        │  2. Built-in Event Hub-compatible endpoint
        │     Consumer group: drone-telemetry
        │     Protocol: AMQP
        ▼
[Dashboard Pod — EventHubConsumerClient]
        │
        │  3. on_event() callback: parse JSON → update drone_state dict
        │     drone_state["drone-alpha"] = {latest telemetry}
        ▼
[Dashboard Pod — Socket.IO]
        │
        │  4. socketio.emit("telemetry", payload)
        │     Protocol: WebSocket over NGINX Ingress
        ▼
[Browser — Leaflet.js + app.js]
        │
        │  5. socket.on("telemetry") → update map marker, telemetry card
        ▼
[Dashboard UI]
```

**Step 1 (Drone → IoT Hub):** Each drone device client connects to IoT Hub using a per-device connection string (AMQP, port 5671, TLS). Messages include a `drone_id` custom property for routing. Telemetry is sent every 5 seconds per drone.

**Step 2 (IoT Hub → Dashboard):** The dashboard connects to IoT Hub's built-in Event Hub-compatible endpoint using the `azure-eventhub` SDK. It subscribes to the `drone-telemetry` consumer group, starting from the latest events (`-1` offset).

**Step 3 (Parse & Store):** The `on_event()` callback parses the JSON payload and updates the in-memory `drone_state` dictionary, keyed by `drone_id`. This dict always holds the latest reading for each active drone.

**Step 4 (Push to Browser):** Every telemetry event is immediately emitted to all connected browsers via Socket.IO WebSocket.

**Step 5 (Browser Update):** The browser-side JavaScript (`app.js`) receives the event and updates the Leaflet map marker, flight trail, signal indicator color, and telemetry card — all in real time.

### Mode 2: Demo Mode (No IoT Hub)

When `DEMO_MODE=true` (or no Event Hub connection string is set), the dashboard replaces the IoT Hub consumer with an internal synthetic data generator:

```
[_DemoDrone objects — in-memory fleet]
        │
        │  tick every 3 seconds
        │  drone.step() → advance state machine
        ▼
[drone_state dict updated directly]
        │
        │  socketio.emit("telemetry", payload)
        ▼
[Browser — same as live mode]
```

The demo generator manages a fleet of `DRONE_COUNT` (default: 5) drones entirely in Python, with no network calls. It reproduces the same lifecycle (launch → patrol → return → charge → replace) and the same telemetry JSON structure as the real simulator.

### AI Insights Flow

The AI analysis runs on a separate background thread, independent of the telemetry flow:

```
[_start_ai_analyzer thread — every 15 seconds]
        │
        │  1. Read drone_state dict (in-memory snapshot)
        │     Build compact text: "drone-alpha [patrolling]: rsrp=-85dBm ..."
        ▼
[Rule Engine — _generate_demo_insights()]
        │
        │  2. Deterministic checks:
        │     RSRP < -100? → coverage warning
        │     Battery < 30%? → battery alert
        │     Latency > 15ms? → performance warning
        │     Packet loss > 1%? → anomaly alert
        │     Fleet-wide averages → predictive insight
        ▼
[Immediate emit — socketio.emit("ai_insights", result)]
        │
        │  3. If EDGE_AI_ENABLED=true:
        │     POST https://phi-4-deployment.foundry-local.svc:5000/v1/chat/completions
        │     Headers: api-key: <foundry-key>
        │     Body: {model, messages, temperature: 0.3, max_tokens: 120}
        ▼
[Phi-4 Mini — GPU Node]
        │
        │  4. Response: one-sentence natural-language fleet summary
        │     e.g. "Drone-charlie has weak signal at -108 dBm near Port Olímpic."
        ▼
[result["summary"] updated → socketio.emit("ai_insights", updated_result)]
        │
        ▼
[Browser — ai_insights panel updated]
```

**Key detail:** The rule engine fires *immediately* and updates the UI before waiting for the AI model. The Phi-4 call then overlays a natural-language summary on top of the rule-engine insights. This makes the UI responsive even when the model is under load.

---

## 4. AI Insights: Running Locally at the Edge

### Why "at the Edge"?

All AI inference happens on the GPU node inside the AKS Arc cluster on-premises. The Phi-4 Mini model:
- Is stored on the GPU node's local disk (downloaded once at operator startup)
- Runs as a container on the GPU node (NVIDIA A2, 16 GB VRAM)
- Is **never called via the internet** — only via an in-cluster Kubernetes service
- Has no dependency on Azure OpenAI or any external inference API

The in-cluster service address is `phi-4-deployment.foundry-local.svc:5000`. Traffic never leaves the physical servers.

### Foundry Local Inference Operator

The **Foundry Local Inference Operator** (Private Preview, v0.0.1-prp.5) is a Kubernetes operator that manages the full lifecycle of the Phi-4 model:

```
[Foundry Local Operator — foundry-local-operator namespace]
        │
        │  watches Model + ModelDeployment CRDs
        ▼
Model CRD (phi-4-mini):
  source:
    type: catalog
    catalog:
      alias: "phi-4-mini"
        │
        │  1. Downloads Phi-4-mini-instruct from catalog
        ▼
ModelDeployment CRD (phi-4-deployment):
  workloadType: generative
  compute: gpu
  replicas: 1
  resources:
    limits:
      gpu: 1          ← pins to NVIDIA A2
  authentication:
    enabled: true     ← auto-generates API key
        │
        │  2. Schedules inference pod on GPU node
        │  3. Creates ClusterIP service (:5000)
        │  4. Creates secret: phi-4-deployment-api-keys
        ▼
[phi-4-deployment pod — running on GPU node]
  Exposes: /v1/chat/completions (OpenAI-compatible API)
  Auth:    api-key header (Foundry-issued key)
  TLS:     self-signed cert (managed by cert-manager + trust-manager)
```

### What Data the AI Model Receives

The model receives a compact text prompt — **not raw JSON** — to ensure the small 14B-parameter model focuses on analysis rather than echoing data:

```
System:
  "You are a drone fleet analyst. Respond with ONE short sentence
   summarising fleet health..."

User:
  "Drone fleet readings:
   drone-alpha [patrolling]: rsrp=-85dBm sinr=18dB lat=4ms loss=0.01% dl=650Mbps bat=72%
   drone-bravo [returning]:  rsrp=-108dBm sinr=3dB  lat=18ms loss=1.2% dl=120Mbps bat=16%
   drone-charlie [patrolling]: rsrp=-78dBm sinr=25dB lat=2ms loss=0% dl=980Mbps bat=55%
   One sentence summary:"
```

The model returns a single sentence, e.g.:
> *"Drone-bravo has weak signal and low battery while returning to base; all other drones nominal."*

Settings used:
- `temperature: 0.3` — low randomness for consistent, factual summaries
- `max_tokens: 120` — short response, fast inference
- No streaming — single synchronous HTTP call

### Two-Layer Reliability Design

The AI analysis pipeline uses two layers to ensure the dashboard always shows useful insights:

| Layer | Technology | Latency | Reliability |
|---|---|---|---|
| **Rule Engine** | Deterministic Python logic | < 1 ms | 100% (no network call) |
| **Phi-4 Summary** | SLM inference on GPU | 1–5 s | Depends on GPU availability |

The rule engine always fires first. If the Phi-4 call fails (model loading, GPU busy, timeout), the dashboard shows rule-engine insights with `status: "fallback"` rather than showing nothing.

### AI Insights Lifecycle (per 15-second cycle)

```
t=0s   Rule engine runs → insights emitted to browser immediately
t=0s   POST to phi-4-deployment (async, 30s timeout)
t=1–5s Phi-4 responds with one-sentence summary
t=1–5s summary field updated → emitted to browser
t=15s  Next cycle begins
```

### Polling Fallback

In addition to WebSocket push, a REST endpoint is available at `/api/ai-insights`. The browser polls this every 15 seconds as a fallback if the WebSocket connection drops. This endpoint returns the latest `ai_insights` dict from memory.

---

## 5. Data Privacy and On-Premises Boundaries

This is a key aspect of the "running at the edge" story. Here is exactly where each piece of data goes:

### Data That Stays On-Premises (Never Leaves the Edge Servers)

| Data | Where It Lives | Who Reads It |
|---|---|---|
| Drone telemetry (demo mode) | `drone_state` dict in dashboard pod memory | Dashboard, AI analyzer |
| AI inference prompts | Dashboard pod → GPU pod (in-cluster HTTPS) | Phi-4 pod only |
| AI inference responses | GPU pod → Dashboard pod (in-cluster) | Dashboard only |
| Phi-4 model weights | GPU node local disk (downloaded once) | Phi-4 pod |
| API keys (Foundry Local) | Kubernetes Secret (`phi-4-deployment-api-keys`) | Dashboard pod only |
| Grafana dashboards | `monitoring` namespace, PVC | Internal only |

### Data That Goes to Azure (IoT Hub Mode Only)

| Data | Destination | Purpose |
|---|---|---|
| Drone telemetry JSON | Azure IoT Hub (AMQP, encrypted) | D2C message routing |
| Device connection strings | Azure Key Vault | Secure secret storage |
| Container images | Azure Container Registry | Image distribution |

**In demo mode (`DEMO_MODE=true`)**, no data is sent to Azure at all. The entire demo is self-contained on the two Lenovo SE350 servers.

### What Azure IoT Hub Stores

When running in live mode, Azure IoT Hub stores telemetry messages temporarily (for the S1 SKU: 1 device-to-cloud partition, 7-day message retention by default). The dashboard reads from the built-in Event Hub endpoint and processes messages in real time; no long-term storage or analytics pipeline is configured in this demo.

---

## 6. Edge Computing: Why This Runs at the Edge

### AKS Arc = Kubernetes on Your Hardware, Managed via Azure

**Azure Kubernetes Service Arc** deploys a standard AKS cluster on Azure Local (HCI) nodes. The cluster is:
- **Physically at the edge** — the SE350 servers are on-site at the event
- **Managed via Azure Arc** — cluster lifecycle, RBAC, policies managed from Azure portal without routing workload traffic through Azure
- **Self-contained** — all workloads (dashboard, simulator, AI, monitoring) run locally

### Why AI at the Edge Matters

Running Phi-4 Mini on a local GPU node (rather than calling Azure OpenAI or another cloud API) provides:

1. **Latency** — In-cluster HTTP call takes < 5 s. A cloud API call would add network round-trip + potential queuing.
2. **Data Sovereignty** — Telemetry data used for AI analysis never leaves the physical servers. For real deployments (e.g., telecoms, industrial, healthcare), this is often a compliance requirement.
3. **Offline Operation** — If the internet connection goes down, all AI insights continue to work. The only dependency on the internet is the IoT Hub connection for receiving live telemetry; in demo mode, the demo runs fully disconnected.
4. **Cost Control** — No per-token cloud API billing. Once the hardware and model are deployed, inference is free.
5. **Consistency** — No rate limits, no quota exhaustion, no shared API throttling during a live demo.

### Edge AI Inference: GPU Utilization

The NVIDIA A2 GPU running Phi-4 Mini:
- **VRAM usage:** ~49% (8 GB of 16 GB) when the model is loaded
- **GPU utilization:** Spikes during inference, visible in Grafana (DCGM Exporter metrics)
- **Temperature:** ~59°C at steady state under inference load
- **Power draw:** ~26.4 W at inference load

This demonstrates that a relatively modest edge GPU can run a capable 14B-parameter language model in real time.

### Network Architecture at the Edge

```
Internet
    │
    │  HTTPS (port 443)
    ▼
DNS: mwc.adaptivecloudlab.com → 172.21.229.201
    │
    ▼
MetalLB VIP: 172.21.229.201 (L2 advertisement, LAN only)
    │
    ▼
NGINX Ingress Controller (pdx-ingress namespace)
  TLS termination (self-signed cert, cert-manager ClusterIssuer)
    │
    ▼
Dashboard Service (ClusterIP)
    │
    ▼
Dashboard Pod (Flask + Socket.IO, port 5000)
    │
    ├──── reads ────> drone_state dict (in-memory or Event Hub)
    │
    └──── calls ────> phi-4-deployment.foundry-local.svc:5000
                          │
                          ▼
                      GPU Node (NVIDIA A2)
                      Phi-4 Mini inference pod
```

The browser only ever contacts `172.21.229.201` (MetalLB VIP). All service-to-service communication (dashboard ↔ AI model) happens over the Kubernetes cluster network — no traffic leaves the physical servers.

---

## 7. Component Reference

### Dashboard Backend (`dashboard/app.py`)

| Function | Role |
|---|---|
| `_start_eventhub_consumer()` | Connects to IoT Hub Event Hub endpoint; updates `drone_state` on each event |
| `_start_demo_generator()` | Manages synthetic drone fleet; ticks every 3 s; updates `drone_state` |
| `_start_ai_analyzer()` | Background thread; runs rule engine + optional Phi-4 call every 15 s |
| `_generate_demo_insights()` | Rule engine: deterministic per-drone + fleet-wide analysis |
| `_build_telemetry_snapshot()` | Converts `drone_state` to compact text for AI prompt |
| `_call_edge_ai(prompt)` | HTTPS POST to Foundry Local; returns one-sentence summary |
| `handle_connect()` | On WebSocket connect: replay current `drone_state` + `ai_insights` to new client |
| `/api/state` | REST: returns full `drone_state` JSON |
| `/api/ai-insights` | REST: returns latest `ai_insights` JSON (polling fallback) |

### IoT Simulator (`iot-simulation/drone-telemetry-simulator.py`)

| Function | Role |
|---|---|
| `Drone.__init__()` | Initialize drone at base with random battery and waypoints |
| `Drone.connect()` | Connect to IoT Hub via AMQP using `IoTHubDeviceClient` |
| `Drone.step()` | Advance state machine by one tick |
| `Drone.build_telemetry()` | Generate telemetry payload with 5G metrics |
| `Drone.send_telemetry()` | Wrap payload in `Message`, set content type, send to IoT Hub |
| `drone_worker(drone)` | Thread function: connect → loop send_telemetry every 5 s → disconnect |

### Kubernetes Manifests (`k8s/`)

| File | Contents |
|---|---|
| `foundry-local.yaml` | `Model` and `ModelDeployment` CRDs for Phi-4 Mini |
| `drone-demo.yaml.template` | Dashboard + Simulator Deployments, Service, Ingress (rendered by deploy script) |
| `metallb-config.yaml` | MetalLB `IPAddressPool` and `L2Advertisement` for VIP `172.21.229.201` |
| `monitoring-values.yaml` | Helm values for `kube-prometheus-stack` |
| `dcgm-values.yaml` | Helm values for NVIDIA DCGM Exporter (GPU metrics) |
| `grafana-dashboard.json` | Pre-built Grafana dashboard: cluster + GPU + drone workloads |

### Scripts (`scripts/`)

| Script | What It Deploys |
|---|---|
| `00-bootstrap-secrets.ps1` | Resource group, Key Vault, SSH keys, passwords |
| `01-create-cluster.ps1` | AKS Arc cluster, system/user/GPU node pools |
| `02-install-platform.ps1` | MetalLB, NGINX Ingress, cert-manager, trust-manager, Foundry Operator, IoT Ops |
| `03-deploy-iot-simulation.ps1` | IoT Hub, device registration, `.env` with connection strings |
| `04-deploy-drone-demo.ps1` | Build + push container images, create K8s secrets, apply manifests |
| `05-deploy-monitoring.ps1` | Prometheus, Grafana, DCGM Exporter, custom dashboard ConfigMap |

---

## Summary

This demo is a complete edge AI platform in a portable, self-contained form:

- **Two Lenovo SE350 servers** host a full 6-node Kubernetes cluster with GPU acceleration
- **Azure is used only for IoT Hub, Key Vault, and ACR** — no Azure compute runs any part of the demo
- **Phi-4 Mini runs entirely on-premises** on the NVIDIA A2 GPU, processing drone telemetry with no data leaving the edge
- **AI insights are generated locally** using a two-layer approach: a deterministic rule engine for instant feedback and a 14B-parameter SLM for natural-language fleet health summaries
- **The dashboard is self-contained** — in demo mode it generates synthetic telemetry, runs the AI, and serves the UI without any internet connectivity
- **All service-to-service communication is in-cluster** — the AI model endpoint, telemetry state, and WebSocket server are all within the same Kubernetes cluster network
