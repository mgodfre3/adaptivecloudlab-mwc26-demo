<#
.SYNOPSIS
    Build, push, and deploy the video analysis dashboard + CV inference to AKS Arc.

.DESCRIPTION
    This script:
    1. Loads deployment config from config/vi-mobile.env
    2. Renders the K8s template (video-dashboard.yaml.template → video-dashboard.yaml)
    3. Builds container images via ACR (no local Docker needed)
    4. Populates K8s secrets from local .env files
    5. Applies the K8s manifests to the cluster

.PARAMETER Tag
    Image tag (default: latest)

.PARAMETER SkipBuild
    Skip ACR build steps (deploy manifests only)

.PARAMETER EnvFile
    Path to the env file. Defaults to config/vi-mobile.env.

.EXAMPLE
    .\scripts\07-deploy-video-dashboard.ps1
    .\scripts\07-deploy-video-dashboard.ps1 -SkipBuild
#>

param(
    [string]$Tag = "latest",
    [switch]$SkipBuild,
    [string]$EnvFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:AZURE_EXTENSION_DIR = "$env:USERPROFILE\.azure\cliext-vi"

# ── Locate & parse env file ──────────────────────────────────────────────────
if (-not $EnvFile) {
    $candidates = @(
        (Join-Path $root "config\vi-mobile.env")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $EnvFile = (Resolve-Path $c).Path; break }
    }
}
if (-not $EnvFile -or -not (Test-Path $EnvFile)) {
    Write-Host "ERROR: Cannot find env file. Ensure config/vi-mobile.env exists." -ForegroundColor Red
    exit 1
}

# Parse key=value pairs (skip comments and blank lines)
$config = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $eqIdx = $line.IndexOf("=")
        if ($eqIdx -gt 0) {
            $key = $line.Substring(0, $eqIdx).Trim()
            $val = $line.Substring($eqIdx + 1).Trim().Trim('"').Trim("'")
            $config[$key] = $val
        }
    }
}

# ── Derive names ─────────────────────────────────────────────────────────────
$prefix = $config["PREFIX"]
if (-not $prefix) { Write-Host "ERROR: PREFIX not set in $EnvFile" -ForegroundColor Red; exit 1 }

$AcrName = "acxcontregwus2"
$AcrLoginServer = "$AcrName.azurecr.io"
$config["ACR_LOGIN_SERVER"] = $AcrLoginServer

# Derive TLS_DNS_BASE from VIDEO_DASHBOARD_HOSTNAME
$dashboardHost = $config["VIDEO_DASHBOARD_HOSTNAME"]
if (-not $dashboardHost) { $dashboardHost = "video.acx.mobile" }
$tlsDnsBase = if ($dashboardHost -match '^[^.]+\.(.+)$') { $Matches[1] } else { $dashboardHost }
$config["TLS_DNS_BASE"] = $tlsDnsBase

Write-Host "`n=== Video Dashboard Deployment ===" -ForegroundColor Cyan
Write-Host "ACR:       $AcrLoginServer"
Write-Host "Tag:       $Tag"
Write-Host "Hostname:  $dashboardHost"
Write-Host "DNS Base:  $tlsDnsBase"
Write-Host "Root:      $root"

# ── Step 0: Render K8s template ──────────────────────────────────────────────
Write-Host "`n[0/7] Rendering K8s template..." -ForegroundColor Yellow
$templatePath = "$root\k8s\video-dashboard.yaml.template"
if (-not (Test-Path $templatePath)) {
    Write-Host "ERROR: $templatePath not found" -ForegroundColor Red
    exit 1
}
$template = Get-Content $templatePath -Raw

foreach ($key in $config.Keys) {
    $template = $template -replace [regex]::Escape("`${$key}"), $config[$key]
}
$template | Set-Content "$root\k8s\video-dashboard.yaml" -Encoding utf8
Write-Host "  Rendered k8s/video-dashboard.yaml"

# ── Step 1: ACR Login ────────────────────────────────────────────────────────
if (-not $SkipBuild) {
    Write-Host "`n[1/7] Logging into ACR ($AcrName)..." -ForegroundColor Yellow
    az acr login --name $AcrName 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "WARNING: ACR login failed — falling back to az acr build (no local Docker needed)" -ForegroundColor DarkYellow
    }

    # ── Step 2: Build & Push Video Dashboard ──────────────────────────────────
    Write-Host "`n[2/7] Building video-dashboard image via ACR..." -ForegroundColor Yellow
    $dashImg = "drone-demo/video-dashboard:$Tag"
    az acr build --registry $AcrName --image $dashImg "$root\video-dashboard"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Video dashboard build failed" -ForegroundColor Red; exit 1 }

    # ── Step 3: Build & Push CV Inference ─────────────────────────────────────
    Write-Host "`n[3/7] Building cv-inference image via ACR..." -ForegroundColor Yellow
    $cvImg = "drone-demo/cv-inference:$Tag"
    $cvDir = "$root\cv-inference"
    if (Test-Path $cvDir) {
        az acr build --registry $AcrName --image $cvImg "$cvDir"
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: CV inference build failed" -ForegroundColor Red; exit 1 }
    } else {
        Write-Host "  ⚠️  cv-inference/ directory not found — skipping CV build" -ForegroundColor Yellow
    }
} else {
    Write-Host "`n[1-3/7] Skipping build/push (--SkipBuild)" -ForegroundColor DarkGray
}

# ── Step 4: Create namespace ─────────────────────────────────────────────────
Write-Host "`n[4/7] Creating namespace video-analysis..." -ForegroundColor Yellow
$nsExists = kubectl get ns video-analysis -o name 2>$null
if ($nsExists) {
    Write-Host "  ⏭️  Namespace video-analysis already exists" -ForegroundColor Green
} else {
    kubectl create namespace video-analysis --dry-run=client -o yaml | kubectl apply -f -
    Write-Host "  ✅ Namespace created" -ForegroundColor Green
}

# ── Step 5: Create secrets ───────────────────────────────────────────────────
Write-Host "`n[5/7] Creating K8s secrets..." -ForegroundColor Yellow

# Parse secrets from local .env files
$dashEnv = "$root\dashboard\.env"
$foundryApiKey = ""
if (Test-Path $dashEnv) {
    $foundryApiKey = (Get-Content $dashEnv | Where-Object { $_ -match "^EDGE_AI_API_KEY=" }) -replace "^EDGE_AI_API_KEY=", ""
}

$viEndpoint = $config["VI_ACCOUNT_NAME"]
if (-not $viEndpoint) { $viEndpoint = "" }

kubectl create secret generic video-dashboard-secrets `
    -n video-analysis `
    --from-literal="EDGE_AI_API_KEY=$foundryApiKey" `
    --from-literal="VI_API_KEY=" `
    --dry-run=client -o yaml | kubectl apply -f -
Write-Host "  ✅ Secrets applied" -ForegroundColor Green

# ── Step 6: Create PVC (if not from template) ────────────────────────────────
# PVC is included in the rendered template — no separate step needed.
# The apply in Step 7 will create it.

# ── Step 7: Apply manifests ──────────────────────────────────────────────────
Write-Host "`n[6/7] Applying K8s manifests..." -ForegroundColor Yellow
kubectl apply -f "$root\k8s\video-dashboard.yaml"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Manifest apply failed" -ForegroundColor Red; exit 1 }

# ── Step 7: Wait for rollout ─────────────────────────────────────────────────
Write-Host "`n[7/7] Waiting for rollout..." -ForegroundColor Yellow
kubectl rollout status deployment/video-dashboard -n video-analysis --timeout=120s

# ── Print status ─────────────────────────────────────────────────────────────
Write-Host "`n=== Deployment Complete ===" -ForegroundColor Green
Write-Host "`nPods:"
kubectl get pods -n video-analysis -o wide

Write-Host "`nIngress:"
kubectl get ingress -n video-analysis

Write-Host "`nPVC:"
kubectl get pvc -n video-analysis

$ingressIP = kubectl get ingress video-dashboard -n video-analysis -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
if ($ingressIP) {
    Write-Host ""
    Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor Green
    Write-Host "│  Dashboard URL: https://$dashboardHost"            -ForegroundColor Green
    Write-Host "│  Ingress IP:    $ingressIP"                        -ForegroundColor Green
    Write-Host "│  DNS Zone:      acx.mobile"                        -ForegroundColor Green
    Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor Green
    Write-Host "  (Point DNS A record for $dashboardHost -> $ingressIP)" -ForegroundColor DarkGray
} else {
    Write-Host "`n  Ingress IP not yet assigned — check with: kubectl get ingress -n video-analysis" -ForegroundColor Yellow
}

Write-Host "`nDone!`n"
