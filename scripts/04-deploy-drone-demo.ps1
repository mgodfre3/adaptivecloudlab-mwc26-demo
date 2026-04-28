<#
.SYNOPSIS
    Build, push, and deploy the drone demo to AKS Arc.

.DESCRIPTION
    This script:
    1. Loads deployment config from config/deployment.env
    2. Renders the K8s template (drone-demo.yaml.template → drone-demo.yaml)
    3. Builds container images via ACR (no local Docker needed)
    4. Populates K8s secrets from local .env files
    5. Applies the K8s manifests to the cluster

.PARAMETER Tag
    Image tag (default: latest)

.PARAMETER SkipBuild
    Skip ACR build steps (deploy manifests only)

.EXAMPLE
    .\scripts\04-deploy-drone-demo.ps1
    .\scripts\04-deploy-drone-demo.ps1 -SkipBuild
#>

param(
    [string]$Tag = "latest",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:AZURE_EXTENSION_DIR = "$env:TEMP\az_extensions"

# ── Load deployment config ───────────────────────────────────────────────────
$deployEnv = "$root\config\deployment.env"
if (-not (Test-Path $deployEnv)) {
    Write-Host "ERROR: $deployEnv not found. Copy config/deployment.env.sample and fill in your values." -ForegroundColor Red
    exit 1
}

# Parse key=value pairs (skip comments and blank lines)
$config = @{}
Get-Content $deployEnv | ForEach-Object {
    if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
        $config[$Matches[1]] = $Matches[2].Trim()
    }
}

$AcrName = $config["ACR_LOGIN_SERVER"]
if (-not $AcrName) {
    Write-Host "ERROR: ACR_LOGIN_SERVER not set in deployment.env" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Drone Demo Deployment ===" -ForegroundColor Cyan
Write-Host "ACR:  $AcrName"
Write-Host "Tag:  $Tag"
Write-Host "Root: $root"

# ── Render K8s template ──────────────────────────────────────────────────────
Write-Host "`n[0/5] Rendering K8s template..." -ForegroundColor Yellow
$template = Get-Content "$root\k8s\drone-demo.yaml.template" -Raw

# Also extract TLS_DNS_BASE from INGRESS_HOSTNAME (strip first subdomain)
$ingressHost = $config["INGRESS_HOSTNAME"]
$tlsDnsBase = if ($ingressHost -match '^[^.]+\.(.+)$') { $Matches[1] } else { $ingressHost }
$config["TLS_DNS_BASE"] = $tlsDnsBase

foreach ($key in $config.Keys) {
    $template = $template -replace [regex]::Escape("`${$key}"), $config[$key]
}
$template | Set-Content "$root\k8s\drone-demo.yaml" -Encoding utf8
Write-Host "  Rendered k8s/drone-demo.yaml"

# ── Step 1: ACR Login ────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    # Extract the short registry name (strip everything from the first dot onward)
    # Handles both "myacr.azurecr.io" and "myacr-guid.azurecr.io" formats
    $AcrRegistryName = ($AcrName -split '\.')[0]

    Write-Host "`n[1/5] Logging into ACR ($AcrRegistryName)..." -ForegroundColor Yellow
    az.cmd acr login --name $AcrRegistryName 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: ACR login failed — falling back to az acr build (no local Docker needed)" -ForegroundColor DarkYellow
    }

    # ── Step 2: Build & Push Dashboard ────────────────────────────────────────
    Write-Host "`n[2/5] Building dashboard image via ACR..." -ForegroundColor Yellow
    $dashImg = "drone-demo/dashboard:$Tag"
    az.cmd acr build --registry $AcrRegistryName --image $dashImg "$root\dashboard"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Dashboard build failed" -ForegroundColor Red; exit 1 }

    # ── Step 3: Build & Push Simulator ────────────────────────────────────────
    Write-Host "`n[3/5] Building simulator image via ACR..." -ForegroundColor Yellow
    $simImg = "drone-demo/simulator:$Tag"
    az.cmd acr build --registry $AcrRegistryName --image $simImg "$root\iot-simulation"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Simulator build failed" -ForegroundColor Red; exit 1 }
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

# Create ACR pull secret using admin credentials (these don't expire, unlike OAuth tokens)
$AcrRegistryName = ($AcrName -split '\.')[0]
Write-Host "  Creating ACR pull secret (admin credentials)..."
$acrCreds = az acr credential show --name $AcrRegistryName -o json 2>&1 | ConvertFrom-Json
if (-not $acrCreds.username) {
    Write-Host "ERROR: Could not retrieve ACR admin credentials. Ensure admin is enabled: az acr update -n $AcrRegistryName --admin-enabled true" -ForegroundColor Red
    exit 1
}
$acrPullArgs = @(
    "create", "secret", "docker-registry", "acr-pull-secret",
    "-n", "drone-demo",
    "--docker-server=$AcrName",
    "--docker-username=$($acrCreds.username)",
    "--docker-password=$($acrCreds.passwords[0].value)",
    "--dry-run=client", "-o", "yaml"
)
& kubectl @acrPullArgs | kubectl apply -f -
Write-Host "  ACR pull secret applied"

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
