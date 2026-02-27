<#
.SYNOPSIS
    Deploys the monitoring stack (Prometheus + Grafana + DCGM GPU exporter) to the AKS Arc cluster.
.DESCRIPTION
    Installs kube-prometheus-stack and NVIDIA DCGM exporter via Helm, provisions the custom
    Grafana dashboard as a ConfigMap, and creates the Ingress for grafana.adaptivecloudlab.com.
#>

param(
    [switch]$SkipHelmInstall,
    [switch]$DashboardOnly
)

$ErrorActionPreference = "Stop"

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent $ScriptDir
$K8sDir     = Join-Path $RepoRoot "k8s"
$Namespace  = "monitoring"

# ── Ensure namespace exists ───────────────────────────────────────────────
Write-Host "`n=== Ensuring namespace '$Namespace' ===" -ForegroundColor Cyan
kubectl create namespace $Namespace --dry-run=client -o yaml | kubectl apply -f -

if (-not $DashboardOnly) {
    # ── Add Helm repos ────────────────────────────────────────────────────
    Write-Host "`n=== Adding Helm repos ===" -ForegroundColor Cyan
    helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>$null
    helm repo add nvidia https://nvidia.github.io/dcgm-exporter/helm-charts 2>$null
    helm repo update

    if (-not $SkipHelmInstall) {
        # ── Deploy kube-prometheus-stack ──────────────────────────────────
        Write-Host "`n=== Deploying kube-prometheus-stack ===" -ForegroundColor Cyan
        helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack `
            --namespace $Namespace `
            --values "$K8sDir\monitoring-values.yaml" `
            --timeout 10m `
            --wait

        # ── Deploy DCGM GPU exporter ─────────────────────────────────────
        Write-Host "`n=== Deploying NVIDIA DCGM Exporter ===" -ForegroundColor Cyan
        helm upgrade --install dcgm-exporter nvidia/dcgm-exporter `
            --namespace $Namespace `
            --values "$K8sDir\dcgm-values.yaml" `
            --timeout 5m `
            --wait
    }
}

# ── Build ConfigMap from dashboard JSON ───────────────────────────────────
Write-Host "`n=== Provisioning Grafana dashboard ConfigMap ===" -ForegroundColor Cyan
$DashboardJson = Get-Content -Raw "$K8sDir\grafana-dashboard.json"

# Escape for YAML multiline literal block
$IndentedJson = ($DashboardJson -split "`n" | ForEach-Object { "    $_" }) -join "`n"

$ConfigMap = @"
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-aks-arc
  namespace: $Namespace
  labels:
    grafana_dashboard: "1"
    app.kubernetes.io/part-of: kube-prometheus-stack
data:
  aks-arc-mwc26.json: |
$IndentedJson
"@

$ConfigMap | kubectl apply -f -

# ── Apply Ingress ─────────────────────────────────────────────────────────
Write-Host "`n=== Applying Grafana Ingress ===" -ForegroundColor Cyan
kubectl apply -f "$K8sDir\grafana-ingress.yaml"

# ── Verify ────────────────────────────────────────────────────────────────
Write-Host "`n=== Monitoring pods ===" -ForegroundColor Cyan
kubectl get pods -n $Namespace -o wide

Write-Host "`n=== Grafana Ingress ===" -ForegroundColor Cyan
kubectl get ingress -n $Namespace

Write-Host "`n`n✅ Monitoring stack deployed!" -ForegroundColor Green
Write-Host "   Grafana URL:  https://grafana.adaptivecloudlab.com" -ForegroundColor Green
Write-Host "   Credentials:  admin / MWC26-Demo!" -ForegroundColor Green
