<#
.SYNOPSIS
    Bootstrap Flux GitOps on an Arc-connected AKS cluster.

.DESCRIPTION
    Installs the microsoft.flux extension and creates a FluxConfiguration
    that watches this repository's gitops/clusters/<ClusterName>/ path.

    Secrets (ACR pull secrets, drone-demo-secrets, etc.) must already exist
    on the cluster — run 00-bootstrap-secrets.ps1 first.

.PARAMETER ClusterName
    Name of the Arc-connected cluster (e.g., pdx-mwc-26, mobile-mwc-26).

.PARAMETER ResourceGroup
    Azure resource group containing the cluster.

.PARAMETER Branch
    Git branch to watch. Defaults to 'main'.

.PARAMETER RepoUrl
    Git repository URL. Defaults to the GitHub repo for this project.

.EXAMPLE
    .\gitops\scripts\setup-flux.ps1 -ClusterName pdx-mwc-26 -ResourceGroup pdx-rg
#>

param(
    [Parameter(Mandatory)]
    [string]$ClusterName,

    [Parameter(Mandatory)]
    [string]$ResourceGroup,

    [string]$Branch = "main",

    [string]$RepoUrl = "https://github.com/mgodfre3/adaptivecloudlab-mwc26-demo"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ── Validate cluster exists ──────────────────────────────────────────────────
Write-Host "`n=== Validating cluster '$ClusterName' in resource group '$ResourceGroup' ===" -ForegroundColor Cyan
$cluster = az connectedk8s show -n $ClusterName -g $ResourceGroup -o json 2>$null | ConvertFrom-Json
if (-not $cluster) {
    Write-Error "Cluster '$ClusterName' not found in resource group '$ResourceGroup'."
    exit 1
}
Write-Host "  Cluster: $($cluster.name) ($($cluster.connectivityStatus))" -ForegroundColor Green

# ── Validate the cluster overlay exists ──────────────────────────────────────
$clusterPath = "gitops/clusters/$ClusterName"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent (Split-Path -Parent $scriptDir)
$localClusterPath = Join-Path $repoRoot $clusterPath

if (-not (Test-Path $localClusterPath)) {
    Write-Error "Cluster overlay not found at '$localClusterPath'. Create it first (see gitops/README.md)."
    exit 1
}
Write-Host "  Cluster overlay: $clusterPath" -ForegroundColor Green

# ── Install Flux extension ───────────────────────────────────────────────────
Write-Host "`n=== Installing microsoft.flux extension ===" -ForegroundColor Cyan
$existingExt = az k8s-extension show `
    --name flux `
    --cluster-name $ClusterName `
    --resource-group $ResourceGroup `
    --cluster-type connectedClusters `
    -o json 2>$null | ConvertFrom-Json

if ($existingExt) {
    Write-Host "  Flux extension already installed (v$($existingExt.version))" -ForegroundColor Yellow
} else {
    Write-Host "  Installing flux extension..."
    az k8s-extension create `
        --name flux `
        --extension-type microsoft.flux `
        --cluster-name $ClusterName `
        --resource-group $ResourceGroup `
        --cluster-type connectedClusters `
        --no-wait
    Write-Host "  Flux extension installation initiated" -ForegroundColor Green
    Write-Host "  Waiting for extension to provision..."
    az k8s-extension wait `
        --name flux `
        --cluster-name $ClusterName `
        --resource-group $ResourceGroup `
        --cluster-type connectedClusters `
        --created `
        --timeout 600
    Write-Host "  Flux extension installed" -ForegroundColor Green
}

# ── Create FluxConfiguration ────────────────────────────────────────────────
Write-Host "`n=== Creating FluxConfiguration 'gitops' ===" -ForegroundColor Cyan

$existingConfig = az k8s-configuration flux show `
    --name gitops `
    --cluster-name $ClusterName `
    --resource-group $ResourceGroup `
    --cluster-type connectedClusters `
    -o json 2>$null | ConvertFrom-Json

if ($existingConfig) {
    Write-Host "  FluxConfiguration 'gitops' already exists — updating..." -ForegroundColor Yellow
    az k8s-configuration flux update `
        --name gitops `
        --cluster-name $ClusterName `
        --resource-group $ResourceGroup `
        --cluster-type connectedClusters `
        --url $RepoUrl `
        --branch $Branch `
        --kustomization name=cluster path=./$clusterPath prune=true sync_interval=60s retry_interval=60s
} else {
    Write-Host "  Creating new FluxConfiguration..."
    az k8s-configuration flux create `
        --name gitops `
        --cluster-name $ClusterName `
        --resource-group $ResourceGroup `
        --cluster-type connectedClusters `
        --namespace flux-system `
        --scope cluster `
        --url $RepoUrl `
        --branch $Branch `
        --kustomization name=cluster path=./$clusterPath prune=true sync_interval=60s retry_interval=60s
}

Write-Host "`n=== GitOps setup complete ===" -ForegroundColor Green
Write-Host @"

  Flux is now watching:
    Repo:   $RepoUrl
    Branch: $Branch
    Path:   $clusterPath

  Changes pushed to '$Branch' will auto-apply within ~60 seconds.

  Check status:
    az k8s-configuration flux show -n gitops -c $ClusterName -g $ResourceGroup -t connectedClusters

  View on-cluster:
    kubectl get kustomizations -n flux-system
    kubectl get helmreleases -A

"@ -ForegroundColor Cyan
