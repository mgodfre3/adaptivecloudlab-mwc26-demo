# GitOps Deployment — Adaptive Cloud Lab MWC26 Demo

Centralized, declarative deployment for the MWC26 demo workloads using
**Flux v2** on **Azure Arc-enabled Kubernetes**.

## Architecture

```
gitops/
├── infrastructure/          # Platform services (Helm-based)
│   ├── base/                #   Shared Helm repos + releases
│   └── overlays/            #   Per-cluster patches (ingress hosts, node selectors)
│       ├── pdx-mwc-26/
│       └── mobile-mwc-26/
├── apps/                    # Application workloads (Kustomize)
│   ├── base/                #   Shared manifests
│   │   ├── drone-demo/
│   │   ├── foundry-local/
│   │   ├── metallb/
│   │   └── kube-proxy-watchdog/
│   └── overlays/            #   Per-cluster config + patches
│       ├── pdx-mwc-26/
│       └── mobile-mwc-26/
├── clusters/                # Entry points — one per cluster
│   ├── pdx-mwc-26/
│   └── mobile-mwc-26/
└── scripts/
    └── setup-flux.ps1       # Bootstrap Flux on a cluster
```

## How It Works

1. **Each cluster** gets a Flux configuration pointing at its `clusters/<name>/`
   directory in this repo.
2. The cluster entry point references the **infrastructure** and **apps**
   overlays for that cluster via Kustomize.
3. Flux watches this repo and **automatically reconciles** changes within
   ~1 minute of a push to `main`.

## Quick Start

### Prerequisites

- AKS Arc cluster created and Arc-connected (via `scripts/01-create-cluster.ps1`)
- Platform extensions installed (Foundry operator, IoT Ops — via `scripts/02-install-platform.ps1`)
- Secrets bootstrapped to the cluster (via `scripts/00-bootstrap-secrets.ps1`)

### Enable GitOps on a Cluster

```powershell
.\gitops\scripts\setup-flux.ps1 -ClusterName pdx-mwc-26 -ResourceGroup pdx-rg
```

This installs the Flux extension and creates a `FluxConfiguration` that watches
`gitops/clusters/pdx-mwc-26/` in this repo.

### Making Changes

1. Edit manifests under `gitops/apps/` or `gitops/infrastructure/`
2. Commit and push to `main`
3. Flux auto-applies within ~60 seconds

### Checking Status

```powershell
# View Flux configuration status
az k8s-configuration flux show -n gitops -c <cluster> -g <rg> -t connectedClusters

# View kustomization status on-cluster
kubectl get kustomizations -n flux-system
```

## What's Managed by GitOps

| Component | Type | Location |
|-----------|------|----------|
| NGINX Ingress | HelmRelease | `infrastructure/base/` |
| cert-manager | HelmRelease | `infrastructure/base/` |
| trust-manager | HelmRelease | `infrastructure/base/` |
| kube-prometheus-stack | HelmRelease | `infrastructure/base/` |
| DCGM GPU Exporter | HelmRelease | `infrastructure/base/` |
| Grafana Ingress | Manifest | `infrastructure/overlays/<cluster>/` |
| Drone Demo App | Kustomize | `apps/base/drone-demo/` |
| Foundry Local (Phi-4) | Kustomize | `apps/base/foundry-local/` |
| MetalLB Config | Kustomize | `apps/base/metallb/` |
| kube-proxy Watchdog | Kustomize | `apps/base/kube-proxy-watchdog/` |

## What's NOT Managed by GitOps

These remain as one-time setup scripts:

- **Cluster creation** (`scripts/01-create-cluster.ps1`)
- **Platform extensions** (`scripts/02-install-platform.ps1`) — Foundry operator,
  IoT Ops extensions are managed by Azure Arc, not Flux
- **Secret bootstrapping** (`scripts/00-bootstrap-secrets.ps1`) — secrets are
  pre-created on-cluster, referenced by GitOps manifests
- **IoT Hub provisioning** (`scripts/03-deploy-iot-simulation.ps1`)
- **ACR image builds** — built and pushed separately

## Adding a New Cluster

1. Copy an existing overlay: `cp -r gitops/apps/overlays/pdx-mwc-26 gitops/apps/overlays/<new-cluster>`
2. Update the overlay's `kustomization.yaml` with cluster-specific values
3. Create a cluster entry: `cp -r gitops/clusters/pdx-mwc-26 gitops/clusters/<new-cluster>`
4. Run `setup-flux.ps1` against the new cluster
