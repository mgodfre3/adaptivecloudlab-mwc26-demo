# 8-Minute Demo Briefing: Real-Time Drone Network Monitoring with Edge AI

**MWC 2026 — Adaptive Cloud Lab**

> **Headline:** *"Real-time edge AI powering live drone network monitoring — all running on two physical servers, zero cloud compute."*

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [The Dashboard & Drones](#the-dashboard--drones)
3. [The AI Model — Why Phi-4 Mini](#the-ai-model--why-phi-4-mini)
4. [Foundry Local — The Edge AI Platform](#foundry-local--the-edge-ai-platform)
5. [IoT Operations & Data Sovereignty](#iot-operations--data-sovereignty)
6. [Observability — Grafana](#observability--grafana)
7. [Why Edge AI Matters](#why-edge-ai-matters)
8. [Demo Flow Checklist](#demo-flow-checklist)

---

## Architecture Overview

**Talk track (Minute 1–2)**

Two **Lenovo ThinkEdge SE350 servers** host a **6-node AKS Arc cluster** on Azure Local:

| Node Role | Count | Purpose |
|---|---|---|
| Control Plane | 1 | Kubernetes API, etcd |
| System Nodes | 2 | NGINX Ingress, cert-manager, MetalLB, monitoring |
| User Nodes | 2 | Dashboard + simulator pods |
| GPU Node | 1 | NVIDIA A2 (16 GB VRAM) — Phi-4 inference |

```
Physical Servers (Azure Local HCI)
└── AKS Arc Cluster
    ├── Control Plane Node    (172.21.229.195)
    ├── System Node 1         ← platform services
    ├── System Node 2         ← platform services
    ├── User Node 1           ← dashboard + simulator
    ├── User Node 2           ← dashboard + simulator
    └── GPU Node (NVIDIA A2)  ← Phi-4 AI inference
```

**Azure is management-plane only** — IoT Hub for device registry, ACR for container images, Key Vault for secrets. **No Azure compute runs any part of the demo.**

### Network Details

| Component | Value |
|---|---|
| Dashboard URL | `https://mwc.adaptivecloudlab.com` |
| Grafana URL | `https://grafana.adaptivecloudlab.com` |
| MetalLB VIP | `172.21.229.201` (L2 advertisement) |
| AI Endpoint (in-cluster) | `https://phi-4-deployment.foundry-local.svc:5000` |
| IoT Ops MQTT (edge mode) | `aio-broker-insecure:1883` |

---

## The Dashboard & Drones

**Talk track (Minute 2–4)**

### Drone Fleet Behavior

**5 simulated drones** patrol 12 Barcelona landmarks (Sagrada Família, Camp Nou, Port Olímpic, Park Güell, and more). Each drone follows an autonomous lifecycle:

1. **Launches** from base station (41.3545°N, 2.1279°E) with a NATO callsign
2. **Patrols** waypoints at 8–18 m/s, reporting 5G telemetry every 3 seconds
3. **Returns** to base when battery drops to 18%
4. **Charges** to 92%, then **retires** — a new drone with the next callsign (Alpha → Bravo → … → Zulu → Alpha) takes over

This creates a continuous, ever-evolving fleet — ideal for long-running kiosk demos.

### Telemetry Payload

Every message includes:

| Category | Metrics |
|---|---|
| **5G Network** | RSRP (dBm), RSRQ (dB), SINR (dB), DL/UL throughput (Mbps), latency (ms), packet loss (%), cell ID, band (n78) |
| **Location** | Latitude, longitude, altitude (m), heading (°), speed (m/s) |
| **Status** | Battery (%), drone lifecycle state, environment (temp, wind, humidity) |

### Three Data Modes

| Mode | Source | Use Case |
|---|---|---|
| **Demo** | In-memory synthetic generator | Offline testing, kiosk fallback — no infrastructure needed |
| **Cloud** | Azure IoT Hub Event Hub endpoint | Production with cloud persistence |
| **Edge** | Azure IoT Operations MQTT broker | Data sovereignty — all telemetry stays on-premises |

The dashboard auto-falls back to demo mode if no Event Hub connection string is configured.

### Dashboard UI

- **Leaflet map** — Dark tile layer centered on Barcelona with live drone markers, flight trails, and color-coded signal indicators (green/yellow/red)
- **Telemetry cards** — Per-drone metrics updated in real time via WebSocket (Socket.IO)
- **Status badges** — PATROLLING (green), RETURNING (yellow), LAUNCHING (blue), CHARGING (cyan), LANDING (orange), EMERGENCY (red)
- **Fleet aggregates** — Bottom bar: average RSRP, DL throughput, latency, active drone count, messages/sec
- **Health indicators** — Top-right: WebSocket connection, AI health, data mode, live clock

---

## The AI Model — Why Phi-4 Mini

**Talk track (Minute 5–6)**

### Model Overview

**Phi-4 Mini Instruct** — Microsoft's compact 14 billion parameter small language model (SLM), purpose-built for constrained edge environments.

### Why Phi-4 Mini Specifically

| Factor | Detail |
|---|---|
| **Fits the GPU** | ~7–8 GB quantized fits in 16 GB A2 VRAM with headroom for K8s + OS overhead |
| **Fast inference** | ~25–30 tokens/sec on NVIDIA A2 → responses in 2–5 seconds |
| **Quality** | Excellent at factual summarization of structured telemetry metrics |
| **Commercial-ready** | Microsoft license, no special on-premises fees |
| **Edge-optimized** | Designed for constrained environments, not a downsized cloud model |

### Two-Layer AI Pipeline

The Edge AI analysis uses a reliability-first approach:

| Layer | Technology | Latency | Reliability |
|---|---|---|---|
| **Rule Engine** | Deterministic Python logic | < 1 ms | 100% — no network call |
| **Phi-4 Summary** | SLM inference on GPU | 2–5 s | Depends on GPU availability |

**How it works:**

1. **Rule Engine fires immediately** — deterministic alerts for signal degradation (RSRP < -100 dBm), battery warnings (< 30%), high latency (> 15 ms), packet loss anomalies (> 1%), and fleet-wide forecasts
2. **Phi-4 overlays asynchronously** — a one-sentence natural-language fleet health summary on top of the structured insights
3. **If the GPU is busy or unavailable**, the rule engine still provides useful insights — **the UI never goes blank**

### What the Model Sees

The prompt is **compact text** (not raw JSON) to keep the 14B model focused on analysis rather than echoing data:

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

**Response:** *"Drone-bravo has weak signal and low battery while returning to base; all other drones nominal."*

**Settings:** `temperature: 0.3` (low randomness), `max_tokens: 120` (fast inference), synchronous HTTP call every 15 seconds.

---

## Foundry Local — The Edge AI Platform

**Talk track (Minute 4–6) — This is the centerpiece of the AI story**

### What Is Foundry Local?

**Foundry Local** is Microsoft's end-to-end local AI solution for running inference entirely on-device — no cloud compute, no per-token costs, no data leaving your hardware. It provides:

| Capability | Detail |
|---|---|
| **Lightweight runtime** | ~20 MB footprint built on [ONNX Runtime](https://onnxruntime.ai/) |
| **Curated model catalog** | Pre-optimized models: Phi-4, Qwen, DeepSeek, Mistral, Whisper, and more |
| **Automatic hardware acceleration** | Detects GPU (NVIDIA CUDA, AMD), NPU, or CPU and selects the best model variant automatically |
| **Smart model management** | Download, cache, load/unload, version-pin — full lifecycle handled |
| **OpenAI-compatible API** | `/v1/chat/completions` — drop-in replacement for Azure OpenAI or OpenAI endpoints |
| **Multi-platform** | Windows, macOS (Intel & Apple Silicon), Linux, Android (preview) |
| **SDKs** | Python, C#, JavaScript, Rust — embed the runtime directly in your app |
| **Bring Your Own Model** | Import custom ONNX models alongside catalog models |

> **Key message:** *"Foundry Local isn't just a model runner — it's a complete local AI platform. Same API as Azure OpenAI, but everything runs on your hardware. Your data never leaves the device."*

### How Foundry Local Runs in This Demo

This demo uses the **Foundry Local Inference Operator** — a Kubernetes-native extension of Foundry Local designed for edge server deployments on AKS Arc. While the standard Foundry Local CLI targets individual devices (laptops, desktops), the inference operator brings the same capabilities to Kubernetes clusters with GPU nodes.

```
┌─────────────────────────────────────────────────────────┐
│  Foundry Local — Two Deployment Models                  │
│                                                         │
│  On-Device (CLI/SDK)          On-Cluster (K8s Operator) │
│  ┌───────────────────┐        ┌───────────────────────┐ │
│  │ foundry model run  │        │ kubectl apply -f      │ │
│  │ foundry service    │        │   foundry-local.yaml  │ │
│  │                    │        │                       │ │
│  │ Windows/macOS/     │        │ AKS Arc + GPU nodes   │ │
│  │ Linux/Android      │        │ (this demo)           │ │
│  └───────────────────┘        └───────────────────────┘ │
│                                                         │
│  Same model catalog · Same ONNX Runtime · Same API      │
└─────────────────────────────────────────────────────────┘
```

### The Inference Operator — Declarative Model Lifecycle

The operator (Private Preview, v0.0.1-prp.5) turns model deployment into a Kubernetes-native workflow. You declare **what** you want; the operator handles **how**.

#### What You Declare (61 lines of YAML)

```yaml
# Step 1: Declare the model — what to download from the catalog
apiVersion: foundrylocal.azure.com/v1
kind: Model
metadata:
  name: phi-3-mini
  namespace: foundry-local
spec:
  displayName: "Phi-3 Mini 4K Instruct"
  description: "Microsoft Phi-3 Mini - compact 3.8B parameter SLM for edge AI inference"
  publisher: "Microsoft"
  source:
    type: catalog
    catalog:
      alias: "phi-3-mini-4k"    # Foundry Local resolves the best GPU variant

# Step 2: Declare the deployment — how to serve it
apiVersion: foundrylocal.azure.com/v1
kind: ModelDeployment
metadata:
  name: phi-3-deployment
  namespace: foundry-local
spec:
  displayName: "Phi-3 Edge AI Deployment"
  model:
    ref: phi-3-mini             # References the Model CRD above
  workloadType: generative
  compute: gpu
  replicas: 1
  resources:
    requests:
      cpu: "1"
      memory: "4Gi"
    limits:
      cpu: "3"
      memory: "5Gi"
      gpu: 1                    # Pin to 1 NVIDIA A2 GPU
  authentication:
    enabled: true               # Auto-generate API key
```

#### What the Operator Does Automatically

| Step | What Happens | You Do Nothing |
|---|---|---|
| 1. **Model download** | Pulls `Phi-3-mini-4k-instruct-cuda-gpu:1` from the Foundry catalog | ✅ Auto |
| 2. **GPU scheduling** | Pins inference pod to the NVIDIA A2 node (`pdxgpu` pool) | ✅ Auto |
| 3. **Service creation** | Creates ClusterIP at `phi-3-deployment.foundry-local.svc:5000` | ✅ Auto |
| 4. **TLS certificates** | Issues self-signed cert via cert-manager + trust-manager | ✅ Auto |
| 5. **API key generation** | Stores key in K8s secret `phi-3-deployment-api-keys` | ✅ Auto |
| 6. **Health monitoring** | Readiness/liveness probes on the inference endpoint | ✅ Auto |
| 7. **OpenAI-compatible API** | Exposes `/v1/chat/completions` — identical to Azure OpenAI | ✅ Auto |

> **Key message:** *"We wrote 61 lines of YAML. Foundry Local downloaded the model, scheduled it on the GPU, set up TLS, generated API keys, and gave us an OpenAI-compatible endpoint — all automatically."*

### Live Demo Commands — Showing Foundry Local in Action

There is no dedicated Foundry Local web dashboard — management happens through `kubectl` and the Kubernetes CRDs. These commands are great for live demo moments:

```powershell
# Show the model resource — what's deployed from the catalog
kubectl get model -n foundry-local
# NAME         AGE
# phi-3-mini   14d

# Show the model deployment — status, replicas, GPU assignment
kubectl get modeldeployment -n foundry-local
# NAME               READY   AGE
# phi-3-deployment   1/1     14d

# Inspect the full deployment details (model variant, compute, auth)
kubectl describe modeldeployment phi-3-deployment -n foundry-local

# Show the running inference pod on the GPU node
kubectl get pods -n foundry-local -o wide
# NAME                              READY   NODE (GPU node)
# phi-3-deployment-xxxxx            1/1     moc-xxxxx (pdxgpu)

# Show the auto-generated inference service endpoint
kubectl get svc -n foundry-local
# NAME               TYPE        CLUSTER-IP      PORT(S)
# phi-3-deployment   ClusterIP   10.x.x.x        5000/TCP

# Retrieve the auto-generated API key (stored securely in K8s)
kubectl get secret phi-3-deployment-api-keys -n foundry-local \
  -o jsonpath='{.data.api-key-primary}' | \
  ForEach-Object { [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($_)) }
# fndry-pk-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# Test the inference endpoint directly (quick health check)
kubectl run test-ai --rm -it --restart=Never --image=curlimages/curl -- \
  curl -sk https://phi-3-deployment.foundry-local.svc:5000/v1/models
```

**For the demo:** Run `kubectl get model,modeldeployment -n foundry-local` to show the model is deployed and healthy in one command.

### Foundry Local Model Catalog

The Foundry catalog includes hardware-optimized models ready for edge deployment:

| Model | Parameters | Use Cases |
|---|---|---|
| **Phi-4 Mini** | 3.8B | Chat, summarization, reasoning (this demo) |
| **Phi-3 Mini 4K** | 3.8B | Chat, summarization (alternative) |
| **Qwen 2.5** | 0.5B–7B | Multilingual chat, coding |
| **DeepSeek R1** | 1.5B–7B | Reasoning, math |
| **Mistral** | 7B | General-purpose chat |
| **Whisper** | various | Speech-to-text transcription |

Models are pre-quantized and optimized per hardware target — Foundry Local automatically selects the right variant (CUDA for NVIDIA, DirectML for AMD, CPU fallback).

### Foundry Local vs. Ollama

The repo includes both options (`foundry-local.yaml` and `edge-ai.yaml`):

| Capability | Foundry Local | Ollama |
|---|---|---|
| **Model lifecycle** | Operator-managed via K8s CRDs | Manual `curl` pull after deploy |
| **Authentication** | Auto-generated API keys | None |
| **TLS** | Auto-provisioned certs (cert-manager) | None |
| **Hardware detection** | Auto-selects best model variant | Manual model selection |
| **API compatibility** | OpenAI-compatible | OpenAI-compatible |
| **Model catalog** | Curated, pre-optimized, versioned | Community hub |
| **Runtime** | ONNX Runtime (~20 MB) | llama.cpp |
| **Production readiness** | Enterprise-grade, K8s-native | Development/testing |
| **BYOM** | ONNX format supported | GGUF format |

> **Key message:** *"We could have used Ollama — it's simpler. But Foundry Local gives us enterprise features out of the box: auto TLS, API key auth, hardware-optimized model selection, and Kubernetes-native lifecycle management. It's the difference between a dev tool and a production platform."*

### GPU Resource Utilization

| Metric | Value |
|---|---|
| GPU Model | NVIDIA A2 (Ampere, 16 GB VRAM) |
| VRAM Usage | ~49% (8 GB of 16 GB) when model loaded |
| GPU Utilization | Spikes every 15 seconds during inference |
| Temperature | ~59°C at steady state |
| Power Draw | ~26.4 W during inference |
| Inference Latency | ~2–5 seconds per call (120 tokens) |

### Foundry Local + Azure OpenAI — Cloud-Edge Symmetry

A key architectural benefit: **the same application code works with both Foundry Local and Azure OpenAI**. The dashboard's AI call is a standard OpenAI-format HTTP POST:

```python
# This code works against BOTH endpoints — just change the URL and key
response = requests.post(
    f"{endpoint}/v1/chat/completions",
    headers={"api-key": api_key},
    json={
        "model": model_name,
        "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
        "temperature": 0.3,
        "max_tokens": 120
    }
)
```

- **Edge (this demo):** `endpoint = https://phi-3-deployment.foundry-local.svc:5000`
- **Cloud (swap anytime):** `endpoint = https://your-resource.openai.azure.com`

> **Key message:** *"Start at the edge with Foundry Local, scale to the cloud with Azure OpenAI — same code, same API, zero refactoring."*

---

## IoT Operations & Data Sovereignty

**Talk track (Minute 6–8)**

### Azure IoT Operations (AIO) on the Cluster

In **edge mode**, Azure IoT Operations provides an on-cluster MQTT broker (`aio-broker`) for telemetry ingestion. The data flow is:

```
Drone Simulator → MQTT publish
        ↓
AIO MQTT Broker (aio-broker:1883)
  ├→ Dashboard subscriber (WebSocket to browser)
  └→ AIO Dataflow (selective cloud export)
          ↓
     Azure IoT Hub (drone-summary topic)
```

**All raw telemetry stays on-premises.** Only anonymized network metrics are exported to the cloud.

### Selective Cloud Export — Data Sovereignty

The AIO Dataflow (`k8s/iot-ops-dataflow.yaml`) transforms data before export:

| Local Field | Cloud Field | Transformation |
|---|---|---|
| `drone_id` | `drone_id` | Pass-through |
| `network.rsrp` | `rsrp_dbm` | Pass-through |
| `network.sinr` | `sinr_db` | Pass-through |
| `network.dl_throughput` | `dl_mbps` | Pass-through |
| `network.latency` | `latency_ms` | Pass-through |
| `network.packet_loss` | `packet_loss_pct` | Pass-through |
| `location.lat` | `area_lat` | **`floor(lat × 100) / 100`** — ~1.1 km precision |
| `location.lon` | `area_lon` | **`floor(lon × 100) / 100`** — ~1.1 km precision |
| Altitude, speed, wind, temp | — | **Dropped entirely** |

**Example:**
- On-premises: `41.36502, 2.15243` (exact coordinates)
- Exported to cloud: `41.36, 2.15` (area-level, ~1.1 km radius)

**Authentication:** Managed identity — no stored credentials for IoT Hub connectivity.

### Key Message

*"Exact drone positions and full sensor payloads never leave these two servers. Only the network quality summary reaches Azure — and even then, GPS is rounded to area-level precision."*

---

## Observability — Grafana

**Show alongside the dashboard throughout the demo**

### What's Deployed

| Component | Purpose |
|---|---|
| **Prometheus** | Metrics collection from all 6 nodes |
| **Grafana** | Dashboard visualization at `https://grafana.adaptivecloudlab.com` |
| **DCGM Exporter** | NVIDIA GPU metrics (runs on GPU node only) |

### Custom Dashboard: "AKS Arc Edge Cluster — MWC 2026"

| Section | Key Metrics |
|---|---|
| **Cluster Overview** | 6 nodes healthy, 203 pods, 19% CPU / 23% memory |
| **NVIDIA GPU - A2** | Utilization gauge, VRAM usage, temperature (°C), power draw (W) |
| **Drone Demo Workloads** | Dashboard/simulator/Foundry pod status, container restarts |
| **Network & Storage** | Node network RX/TX, disk usage, disk I/O |

**What to point out:** GPU utilization spiking every 15 seconds — those are the AI inference bursts from Phi-4 analyzing the drone fleet. A modest edge GPU running real AI at ~26W power draw.

---

## Why Edge AI Matters

**Closing talking points**

| Benefit | Detail |
|---|---|
| **Latency** | In-cluster HTTP call < 5 seconds vs. cloud round-trip + queuing |
| **Data Sovereignty** | Telemetry analyzed on-prem; only anonymized metrics exported |
| **Offline Resilience** | Works without internet — demo mode runs fully disconnected |
| **Cost Control** | No per-token cloud API billing; GPU owned, inference is free |
| **Compliance** | Exact GPS never leaves the edge — GDPR and telecom-friendly |
| **Consistency** | No rate limits, quota exhaustion, or shared API throttling during a live demo |

---

## Demo Flow Checklist

### Pre-Demo (Day Before)

- [ ] Infrastructure deployed (scripts 00–05 completed)
- [ ] Dashboard accessible at `https://mwc.adaptivecloudlab.com`
- [ ] Grafana accessible at `https://grafana.adaptivecloudlab.com`
- [ ] All pods running: `kubectl get pods -n drone-demo -n foundry-local -n monitoring`
- [ ] Foundry Local model healthy: `kubectl get model,modeldeployment -n foundry-local` shows `READY 1/1`
- [ ] GPU metrics appearing in Grafana (VRAM ~49%, utilization spiking every 15s)
- [ ] AI insights updating in the dashboard (check top-right AI health badge)
- [ ] Drones actively patrolling on the map

### 8-Minute Demo Flow

| Time | Show | Say |
|---|---|---|
| **0–2 min** | Dashboard map + architecture | *"5 autonomous drones patrolling Barcelona on a real K8s cluster running on two servers in this room."* |
| **2–3 min** | Telemetry cards + fleet stats | *"Real-time 5G metrics — signal strength, throughput, latency. Drones return to base at low battery and are replaced."* |
| **3–4 min** | AI insights panel + Grafana GPU | *"Watch the AI insights update every 15 seconds. That's Phi-3 Mini running on the GPU — see the utilization spike in Grafana."* |
| **4–6 min** | `kubectl` Foundry Local commands | *"Foundry Local is the platform making this possible. 61 lines of YAML — model download, GPU scheduling, TLS, API keys — all automatic."* Run `kubectl get model,modeldeployment -n foundry-local` and `kubectl describe modeldeployment phi-3-deployment -n foundry-local`. |
| **6–7 min** | Dataflow YAML + architecture diagram | *"Raw data stays on-prem. Only anonymized metrics reach Azure. GPS is rounded to 1km precision before export."* |
| **7–8 min** | Code snippet (cloud-edge symmetry) | *"Same OpenAI API format. Start at the edge with Foundry Local, scale to the cloud with Azure OpenAI — zero refactoring."* |

### Closing Statement

> *"This demo shows what's possible when you combine Foundry Local, AKS Arc, and Azure IoT Operations. Enterprise-grade AI inference at the edge — same API as Azure OpenAI, full data sovereignty, no cloud compute required. 61 lines of YAML to go from zero to a production AI endpoint."*

### Fallback Options

| Issue | Fallback |
|---|---|
| Dashboard not responding | Show README.md screenshots and architecture diagrams |
| GPU unavailable | Show historical Grafana metrics; explain dataflow conceptually |
| Network issues | Demonstrate demo mode — runs fully offline with synthetic drones |
