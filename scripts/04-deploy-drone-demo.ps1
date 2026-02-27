<#
.SYNOPSIS
    Build, push, and deploy the drone demo to AKS Arc.

.DESCRIPTION
    This script:
    1. Builds Docker images for the dashboard and simulator
    2. Pushes them to ACR
    3. Populates the K8s secrets from local .env files
    4. Applies the K8s manifests to the cluster

.PARAMETER AcrName
    ACR login server (e.g. myacr.azurecr.io)

.PARAMETER Tag
    Image tag (default: latest)

.PARAMETER SkipBuild
    Skip Docker build/push steps (deploy manifests only)

.EXAMPLE
    .\scripts\04-deploy-drone-demo.ps1
    .\scripts\04-deploy-drone-demo.ps1 -SkipBuild
#>

param(
    [string]$AcrName = "acxcontregwus2-c6chcgfjardafsb5.azurecr.io",
    [string]$Tag = "latest",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:AZURE_EXTENSION_DIR = "$env:TEMP\az_extensions"

Write-Host "`n=== Drone Demo Deployment ===" -ForegroundColor Cyan
Write-Host "ACR:  $AcrName"
Write-Host "Tag:  $Tag"
Write-Host "Root: $root"

# ── Step 1: ACR Login ────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Host "`n[1/5] Logging into ACR..." -ForegroundColor Yellow
    az.cmd acr login --name $AcrName 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: ACR login failed. Ensure Docker Desktop is running and you have ACR push access." -ForegroundColor Red
        exit 1
    }

    # ── Step 2: Build & Push Dashboard ────────────────────────────────────────
    Write-Host "`n[2/5] Building dashboard image..." -ForegroundColor Yellow
    $dashImg = "$AcrName/drone-demo/dashboard:$Tag"
    docker build -t $dashImg "$root\dashboard"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Dashboard build failed" -ForegroundColor Red; exit 1 }
    
    Write-Host "Pushing $dashImg..."
    docker push $dashImg
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Dashboard push failed" -ForegroundColor Red; exit 1 }

    # ── Step 3: Build & Push Simulator ────────────────────────────────────────
    Write-Host "`n[3/5] Building simulator image..." -ForegroundColor Yellow
    $simImg = "$AcrName/drone-demo/simulator:$Tag"
    docker build -t $simImg "$root\iot-simulation"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Simulator build failed" -ForegroundColor Red; exit 1 }
    
    Write-Host "Pushing $simImg..."
    docker push $simImg
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Simulator push failed" -ForegroundColor Red; exit 1 }
} else {
    Write-Host "`n[1-3/5] Skipping build/push (--SkipBuild)" -ForegroundColor DarkGray
}

# ── Step 4: Populate & apply secrets ─────────────────────────────────────────
Write-Host "`n[4/5] Creating K8s secrets from local .env files..." -ForegroundColor Yellow

# Read secrets from local .env files
$dashEnv = "$root\dashboard\.env"
$simEnv  = "$root\iot-simulation\.env"

# Parse the Foundry Local API key
$apiKey = ""
if (Test-Path $dashEnv) {
    $apiKey = (Get-Content $dashEnv | Where-Object { $_ -match "^EDGE_AI_API_KEY=" }) -replace "^EDGE_AI_API_KEY=", ""
}

# Parse drone connection strings
$droneConnStrs = @{}
if (Test-Path $simEnv) {
    for ($i = 1; $i -le 5; $i++) {
        $line = Get-Content $simEnv | Where-Object { $_ -match "^DRONE_${i}_CONNECTION_STRING=" }
        if ($line) {
            $droneConnStrs[$i] = ($line -replace "^DRONE_${i}_CONNECTION_STRING=", "").Trim()
        }
    }
}

# Create namespace first (idempotent)
kubectl create namespace drone-demo --dry-run=client -o yaml | kubectl apply -f -

# Create the secret
$secretArgs = @(
    "create", "secret", "generic", "drone-demo-secrets",
    "-n", "drone-demo",
    "--from-literal=EDGE_AI_API_KEY=$apiKey",
    "--from-literal=EVENTHUB_CONNECTION_STRING=",
    "--dry-run=client", "-o", "yaml"
)
for ($i = 1; $i -le 5; $i++) {
    $cs = if ($droneConnStrs.ContainsKey($i)) { $droneConnStrs[$i] } else { "" }
    $secretArgs += "--from-literal=DRONE_${i}_CONNECTION_STRING=$cs"
}
& kubectl @secretArgs | kubectl apply -f -
Write-Host "  Secrets applied"

# ── Step 5: Apply manifests ──────────────────────────────────────────────────
Write-Host "`n[5/5] Applying K8s manifests..." -ForegroundColor Yellow
kubectl apply -f "$root\k8s\drone-demo.yaml"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Manifest apply failed" -ForegroundColor Red; exit 1 }

# ── Wait for rollout ─────────────────────────────────────────────────────────
Write-Host "`nWaiting for dashboard rollout..."
kubectl rollout status deployment/dashboard -n drone-demo --timeout=120s

Write-Host "Waiting for simulator rollout..."
kubectl rollout status deployment/simulator -n drone-demo --timeout=120s

# ── Print status ─────────────────────────────────────────────────────────────
Write-Host "`n=== Deployment Complete ===" -ForegroundColor Green
Write-Host "`nPods:"
kubectl get pods -n drone-demo -o wide

Write-Host "`nIngress:"
kubectl get ingress -n drone-demo

$ingressIP = kubectl get ingress dashboard -n drone-demo -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
if ($ingressIP) {
    Write-Host "`n  Dashboard URL: https://adaptivecloudlab.com" -ForegroundColor Cyan
    Write-Host "  Ingress IP:    $ingressIP" -ForegroundColor Cyan
    Write-Host "  (Point DNS A record for adaptivecloudlab.com -> $ingressIP)" -ForegroundColor DarkGray
} else {
    Write-Host "`n  Ingress IP not yet assigned — check with: kubectl get ingress -n drone-demo" -ForegroundColor Yellow
}

Write-Host "`nDone!`n"
