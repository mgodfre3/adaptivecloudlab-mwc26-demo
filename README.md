# Real-Time Drone Network Monitoring with Edge AI

**MWC 2026 Demo — Adaptive Cloud Lab**

A live kiosk demo showing five autonomous drones monitoring 5G network quality across the Barcelona MWC venue area. Telemetry flows through Azure IoT Hub, while a small language model (Phi-3 Mini) running on an NVIDIA GPU at the edge provides real-time AI-powered insights — all orchestrated on AKS Arc (Azure Local).

![Architecture: Drones → IoT Hub → Dashboard ← Foundry Local AI (GPU)](docs/architecture.png)

---

## Architecture Overview

```
┌──────────────┐     D2C telemetry      ┌────────────────┐
│  Drone Sim   │ ────────────────────►  │  Azure IoT Hub │
│  (5 drones)  │   (connection strings) │  (S1, SoCal)   │
└──────────────┘                        └───────┬────────┘
                                                │ Event Hub
                                                ▼
                                      ┌──────────────────┐
                                      │   Flask Dashboard │◄───── Browser (Leaflet map)
                                      │   + Socket.IO     │
                                      └────────┬─────────┘
                                               │ HTTPS /v1/chat/completions
                                               ▼
                                      ┌──────────────────┐
                                      │  Foundry Local    │
                                      │  Phi-3 Mini 4K    │  ◄── NVIDIA A2 GPU
                                      │  (AKS Arc node)   │
                                      └──────────────────┘
```

**Key components:**

| Component | Description |
|---|---|
| **AKS Arc (Azure Local)** | Kubernetes cluster on 2× Lenovo SE350 with NVIDIA A2 GPU |
| **Foundry Local Inference Operator** | Private Preview operator that manages SLM lifecycle on GPU nodes |
| **Phi-3 Mini 4K Instruct** | Microsoft 3.8B-parameter SLM for edge AI inference |
| **Azure IoT Hub** | Cloud-managed device registry and D2C telemetry ingestion |
| **Drone Telemetry Simulator** | Python script simulating 5 drones with 5G telemetry |
| **Live Dashboard** | Flask + Socket.IO + Leaflet.js real-time kiosk UI |

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| **Azure subscription** | Contributor role | For IoT Hub and AKS Arc resources |
| **Azure Local (HCI) cluster** | 2+ nodes with GPU | NVIDIA A2 or similar; `Standard_NC4_A2` VM SKU |
| **Azure CLI** | 2.60+ | With extensions: `connectedk8s`, `aksarc`, `azure-iot-ops`, `azure-iot` |
| **kubectl** | 1.28+ | Access via `az connectedk8s proxy` |
| **Helm** | 3.12+ | For cert-manager, trust-manager, Foundry operator |
| **Python** | 3.10+ | For simulator and dashboard |
| **Node/npm** | Optional | Not required for this demo |

---

## Quick Start (Run on Another Machine)

> **TL;DR** — If infrastructure is already deployed, skip to [Step 4](#step-4-run-the-dashboard).

### Step 1: Clone and configure

```powershell
git clone https://github.com/<org>/adaptivecloudlab-mwc26-demo.git
cd adaptivecloudlab-mwc26-demo

# Copy and fill in your environment config
cp config/aks_arc_cluster.env.sample config/aks_arc_cluster.env
# Edit config/aks_arc_cluster.env with your subscription, custom location, vnet, etc.
```

### Step 2: Deploy infrastructure (one-time)

Run the scripts in order. Each script is idempotent (safe to re-run).

```powershell
# Fix known az CLI extension directory issue on restricted hosts
$env:AZURE_EXTENSION_DIR = "$env:TEMP\az_extensions"

# 1. Create resource group, Key Vault, SSH keys, passwords
.\scripts\00-bootstrap-secrets.ps1

# 2. Create AKS Arc cluster with system, user, and GPU node pools
.\scripts\01-create-cluster.ps1

# 3. Install platform: NGINX ingress, cert-manager, trust-manager, Foundry Local, IoT Ops
.\scripts\02-install-platform.ps1

# 4. Deploy IoT Hub, register drone devices, generate simulator .env
.\scripts\03-deploy-iot-simulation.ps1
```

### Step 3: Deploy Foundry Local AI model

After the operator is installed, apply the model manifests:

```powershell
# Connect to the cluster
az connectedk8s proxy --name <cluster> --resource-group <rg>

# Deploy the Phi-3 model on the GPU node
kubectl apply -f k8s/foundry-local.yaml

# Watch the model download and deployment (takes ~3-5 min)
kubectl get modeldeployment -n foundry-local -w

# Verify the inference service is running
kubectl get svc -n foundry-local
```

> **Important:** If the Helm OCI install fails with `pending-install`, use the bundled `.tgz`:
> ```powershell
> helm install inference-operator ./inference-operator-0.0.1-prp.5.tgz \
>     -n foundry-local-operator --create-namespace --timeout 5m
> ```

### Step 4: Run the dashboard

```powershell
# Set up Python virtual environment
cd dashboard
python -m venv .venv
.venv\Scripts\Activate.ps1      # Windows
# source .venv/bin/activate     # Linux/macOS

pip install -r requirements.txt

# Copy and configure the dashboard environment
cp .env.sample .env
# Edit .env — fill in EDGE_AI_API_KEY and EDGE_AI_ENDPOINT

# Port-forward the Foundry Local inference service (in a separate terminal)
kubectl port-forward svc/phi-3-deployment -n foundry-local 8443:5000

# Run the dashboard
python app.py
```

Open **http://localhost:5000** in a browser. The dashboard shows:
- Live Leaflet map of Barcelona with drone positions
- Real-time 5G telemetry cards (RSRP, RSRQ, SINR, throughput)
- AI-powered fleet insights from Phi-3 (updated every 15 seconds)
- Aggregate network statistics

### Step 5: Run the drone simulator (optional — dashboard has demo mode)

```powershell
cd iot-simulation

# Install dependencies
pip install azure-iot-device python-dotenv

# Ensure .env has connection strings (generated by script 03)
python drone-telemetry-simulator.py
```

---

## Project Structure

```
adaptivecloudlab-mwc26-demo/
├── README.md                           # This file
├── config/
│   └── aks_arc_cluster.env.sample      # Environment config template
├── scripts/
│   ├── 00-bootstrap-secrets.ps1        # RG, Key Vault, SSH keys, passwords
│   ├── 01-create-cluster.ps1           # AKS Arc cluster + node pools
│   ├── 02-install-platform.ps1         # Ingress, cert-mgr, Foundry, IoT Ops
│   └── 03-deploy-iot-simulation.ps1    # IoT Hub, device registration
├── k8s/
│   └── foundry-local.yaml             # Foundry Local Model + ModelDeployment CRDs
├── dashboard/
│   ├── app.py                          # Flask backend (telemetry + AI analysis)
│   ├── requirements.txt                # Python dependencies
│   ├── Dockerfile                      # Container build for dashboard
│   ├── .env.sample                     # Dashboard env template (no secrets)
│   ├── templates/
│   │   └── index.html                  # Main HTML template
│   └── static/
│       ├── css/style.css               # Dark-theme kiosk styles
│       └── js/app.js                   # Leaflet map + Socket.IO client
├── iot-simulation/
│   ├── drone-telemetry-simulator.py    # 5-drone IoT Hub telemetry simulator
│   ├── iot-hub-deployment.bicep        # IoT Hub Bicep template
│   └── iot-device-creation.bicep       # Documentation (devices via CLI)
└── inference-operator-0.0.1-prp.5.tgz  # Foundry Local Helm chart (Private Preview)
```

---

## Dashboard Features

- **Dark-theme kiosk mode** — designed for large screens / event booths
- **Real-time Leaflet map** — drone positions on a dark tile layer centered on Barcelona (Fira Gran Via)
- **Telemetry cards** — per-drone 5G metrics: RSRP (dBm), RSRQ (dB), SINR (dB), throughput (Mbps), battery %, altitude
- **AI Insights panel** — Phi-3 analyzes fleet telemetry every 15 seconds and returns JSON insights (status, recommendation, affected drones)
- **Aggregate statistics** — fleet-wide averages and status summary
- **Demo mode** — runs entirely with synthetic data when `DEMO_MODE=true` (no IoT Hub connection needed)

---

## Foundry Local Details

The demo uses **Foundry Local Inference Operator** (Private Preview) to run Phi-3 Mini on a GPU node.

| Setting | Value |
|---|---|
| Operator version | `0.0.1-prp.5` |
| Chart | `inference-operator-0.0.1-prp.5.tgz` (bundled) |
| Namespace (operator) | `foundry-local-operator` |
| Namespace (workloads) | `foundry-local` |
| Model catalog alias | `phi-3-mini-4k` |
| Model variant | `Phi-3-mini-4k-instruct-cuda-gpu:1` |
| GPU | NVIDIA A2 (Ampere, 16 GB VRAM) |
| Service | `phi-3-deployment.foundry-local.svc:5000` (ClusterIP) |
| Auth | API key via `api-key` header |

**Dependencies:** cert-manager v1.19.2, trust-manager v0.20.3 (with `--secret-targets-enabled`).

### trust-manager patch (required)

After installing trust-manager, enable secret targets:

```powershell
# Add the --secret-targets-enabled arg
kubectl -n cert-manager patch deployment trust-manager --type=json -p '[
  {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--secret-targets-enabled"}
]'

# Create RBAC for secret targets
kubectl apply -f - <<'EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: trust-manager-secret-targets
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get","list","watch","create","update","patch","delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: trust-manager-secret-targets
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: trust-manager-secret-targets
subjects:
  - kind: ServiceAccount
    name: trust-manager
    namespace: cert-manager
EOF
```

---

## IoT Hub Configuration

| Setting | Value |
|---|---|
| Hub name | `${PREFIX}-iothub` (e.g. `pdx-iothub`) |
| SKU | S1 (1 unit) |
| Region | `southcentralus` |
| Devices | `drone-1` through `drone-5` |
| Consumer group | `drone-telemetry` |
| Connection strings | Stored in Key Vault as `${PREFIX}-drone-N-connstr` |

---

## Configuration Reference

### `config/aks_arc_cluster.env`

Primary configuration file. All resource names are auto-derived from `PREFIX`. Key variables:

| Variable | Description | Example |
|---|---|---|
| `PREFIX` | Naming prefix for all resources | `pdx` |
| `SUBSCRIPTION_ID` | Azure subscription GUID | `fbaf508b-...` |
| `CUSTOM_LOCATION_ID` | Azure Local custom location resource ID | `/subscriptions/.../customlocations/portland` |
| `AZURE_METADATA_LOCATION` | Azure region for metadata | `southcentralus` |
| `KUBERNETES_VERSION` | Target K8s version (≥1.29) | `1.32.6` |
| `VNET_RESOURCE_ID` | Azure Local logical network | `/subscriptions/.../logicalnetworks/pdx-lnet-vlan32` |
| `AAD_ADMIN_GROUP_IDS` | Entra ID group object IDs for RBAC | `be0c17dc-...,f5157bd2-...` |
| `DRONE_COUNT` | Number of simulated drones | `5` |
| `GPU_POOL_VM_SIZE` | GPU node SKU | `Standard_NC4_A2` |

### `dashboard/.env`

| Variable | Description | Default |
|---|---|---|
| `DEMO_MODE` | Use synthetic data (no IoT Hub) | `true` |
| `EDGE_AI_ENABLED` | Enable Foundry Local AI insights | `true` |
| `EDGE_AI_ENDPOINT` | Foundry Local API URL | `https://localhost:8443` |
| `EDGE_AI_MODEL` | Model name for inference | `Phi-3-mini-4k-instruct-cuda-gpu:1` |
| `EDGE_AI_API_KEY` | API key for Foundry Local | *(from K8s secret)* |
| `EDGE_AI_INTERVAL` | Seconds between AI analysis cycles | `15` |
| `DRONE_COUNT` | Number of drones in demo mode | `5` |
| `DASHBOARD_PORT` | HTTP port | `5000` |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `az` commands fail with extension errors | Set `$env:AZURE_EXTENSION_DIR = "$env:TEMP\az_extensions"` |
| PowerShell alias conflicts with `az` | Use `az.cmd` instead of `az` |
| `kubectl` auth errors on Arc cluster | Use `az connectedk8s proxy --name <cluster> --resource-group <rg>` (no `--token` flag) |
| Foundry Helm stuck in `pending-install` | Uninstall with `helm uninstall`, then install from local `.tgz` file |
| trust-manager `SecretTargetsDisabled` | Apply the RBAC + deployment patch in [trust-manager patch](#trust-manager-patch-required) |
| Model catalog alias not found | Use `phi-3-mini-4k` (not `phi-3-mini-4k-instruct`) |
| AI insights empty or malformed | Expected — Phi-3 output is variable; the dashboard has multi-layer JSON repair and text fallback |
| Dashboard shows no data | Check `DEMO_MODE=true` in `.env` and that the port-forward is running |

---

## Hardware Reference

**2× Lenovo ThinkEdge SE350** (Azure Local cluster)

| Spec | Value |
|---|---|
| RAM | 128 GB per node (256 GB total) |
| GPU | 1× NVIDIA A2 per node (16 GB VRAM, Ampere) |
| K8s nodes | 6 total: 2 system, 2 user, 2 GPU |
| OS | Azure Linux 3.0 |

---

## License

Internal Microsoft demo — Adaptive Cloud Lab, MWC 2026.