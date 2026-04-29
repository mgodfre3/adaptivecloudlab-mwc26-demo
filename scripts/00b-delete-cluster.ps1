<#
.SYNOPSIS
    Delete an AKS Arc cluster (and optionally its node pools) on Azure Local.

.DESCRIPTION
    Reads config/aks_arc_cluster.env (or a supplied env file), derives all names
    from PREFIX, then:
      1. Confirms the deletion with the operator (unless -Force is specified)
      2. Deletes the GPU node pool
      3. Deletes the user node pool
      4. Deletes the AKS Arc cluster

    The resource group and Key Vault are intentionally NOT deleted so that
    secrets and infrastructure can be re-used when redeploying.

    After this script completes, run:
      scripts/01-create-cluster.ps1   — to redeploy the cluster
      scripts/02-install-platform.ps1 — to reinstall platform components

.PARAMETER EnvFile
    Path to the env file.  Defaults to config/aks_arc_cluster.env,
    then config/vi-portland.env, then config/aks_arc_cluster.env.sample.

.PARAMETER Force
    Skip the confirmation prompt.

.EXAMPLE
    .\scripts\00b-delete-cluster.ps1
    .\scripts\00b-delete-cluster.ps1 -EnvFile config\vi-portland.env -Force
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$EnvFile,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Locate & parse env file ──────────────────────────────────────────────────
if (-not $EnvFile) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\config\aks_arc_cluster.env"),
        (Join-Path $PSScriptRoot "..\config\vi-portland.env"),
        (Join-Path $PSScriptRoot "..\config\aks_arc_cluster.env.sample")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $EnvFile = (Resolve-Path $c).Path; break }
    }
}
if (-not $EnvFile -or -not (Test-Path $EnvFile)) {
    Write-Error "Cannot find env file. Specify -EnvFile or ensure config/aks_arc_cluster.env exists."
    exit 1
}

Write-Host "📄 Loading env from: $EnvFile" -ForegroundColor Cyan

$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#")) {
        $eqIdx = $line.IndexOf("=")
        if ($eqIdx -gt 0) {
            $key = $line.Substring(0, $eqIdx).Trim()
            $val = $line.Substring($eqIdx + 1).Trim().Trim('"').Trim("'")
            $envVars[$key] = $val
        }
    }
}

# ── Derive names from PREFIX ─────────────────────────────────────────────────
$prefix         = $envVars["PREFIX"]
if (-not $prefix) { Write-Error "PREFIX is not set in $EnvFile"; exit 1 }

$subscriptionId = $envVars["SUBSCRIPTION_ID"]
$rgName         = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$clusterName    = if ($envVars["CLUSTER_NAME"])        { $envVars["CLUSTER_NAME"] }        else { "$prefix-aks" }

$sysPoolName    = if ($envVars["SYSTEM_POOL_NAME"])    { $envVars["SYSTEM_POOL_NAME"] }    else { "${prefix}system" }
$userPoolName   = if ($envVars["USER_POOL_NAME"])      { $envVars["USER_POOL_NAME"] }      else { "${prefix}user" }
$gpuPoolName    = if ($envVars["GPU_POOL_NAME"])       { $envVars["GPU_POOL_NAME"] }       else { "${prefix}gpu" }

if (-not $subscriptionId) { Write-Error "SUBSCRIPTION_ID is not set in $EnvFile"; exit 1 }

# ── Display plan ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor DarkRed
Write-Host "│  ⚠️  CLUSTER DELETION PLAN                        │" -ForegroundColor DarkRed
Write-Host "│──────────────────────────────────────────────────│" -ForegroundColor DarkRed
Write-Host "│  Prefix:           $prefix"                         -ForegroundColor DarkRed
Write-Host "│  Subscription:     $subscriptionId"                 -ForegroundColor DarkRed
Write-Host "│  Resource Group:   $rgName  (NOT deleted)"          -ForegroundColor DarkRed
Write-Host "│  Cluster:          $clusterName  ← will be deleted" -ForegroundColor DarkRed
Write-Host "│  Node pools:       $gpuPoolName, $userPoolName"     -ForegroundColor DarkRed
Write-Host "│                    (deleted with cluster)"          -ForegroundColor DarkRed
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor DarkRed
Write-Host ""

# ── Confirmation ─────────────────────────────────────────────────────────────
if (-not $Force) {
    $confirm = Read-Host "Type the cluster name '$clusterName' to confirm deletion"
    if ($confirm -ne $clusterName) {
        Write-Host "❌ Confirmation did not match. Aborting." -ForegroundColor Red
        exit 1
    }
}

# ── Step 1: Set subscription ─────────────────────────────────────────────────
Write-Host "🔑 Setting subscription..." -ForegroundColor Yellow
az account set --subscription $subscriptionId
if ($LASTEXITCODE -ne 0) { Write-Error "az account set failed"; exit 1 }

# ── Step 2: Check cluster exists ─────────────────────────────────────────────
Write-Host ""
Write-Host "🔍 Checking cluster '$clusterName' in '$rgName'..." -ForegroundColor Yellow
$clusterExists = az aksarc show --name $clusterName --resource-group $rgName --query "name" -o tsv 2>$null
if (-not $clusterExists) {
    Write-Host "  ℹ️  Cluster '$clusterName' not found — nothing to delete." -ForegroundColor DarkGray
    exit 0
}
Write-Host "  Found cluster: $clusterExists" -ForegroundColor DarkYellow

# ── Step 3: Delete GPU node pool ─────────────────────────────────────────────
Write-Host ""
Write-Host "🗑️  Deleting GPU node pool '$gpuPoolName'..." -ForegroundColor Yellow
$gpuPoolExists = az aksarc nodepool show --cluster-name $clusterName --resource-group $rgName --name $gpuPoolName --query "name" -o tsv 2>$null
if ($gpuPoolExists) {
    az aksarc nodepool delete `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --name $gpuPoolName `
        --yes `
        --output none
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "⚠️  Failed to delete GPU node pool '$gpuPoolName'. Continuing..."
    } else {
        Write-Host "  ✅ GPU node pool deleted" -ForegroundColor Green
    }
} else {
    Write-Host "  ⏭️  GPU node pool '$gpuPoolName' not found — skipping" -ForegroundColor DarkGray
}

# ── Step 4: Delete user node pool ────────────────────────────────────────────
Write-Host ""
Write-Host "🗑️  Deleting user node pool '$userPoolName'..." -ForegroundColor Yellow
$userPoolExists = az aksarc nodepool show --cluster-name $clusterName --resource-group $rgName --name $userPoolName --query "name" -o tsv 2>$null
if ($userPoolExists) {
    az aksarc nodepool delete `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --name $userPoolName `
        --yes `
        --output none
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "⚠️  Failed to delete user node pool '$userPoolName'. Continuing..."
    } else {
        Write-Host "  ✅ User node pool deleted" -ForegroundColor Green
    }
} else {
    Write-Host "  ⏭️  User node pool '$userPoolName' not found — skipping" -ForegroundColor DarkGray
}

# ── Step 5: Delete the cluster ────────────────────────────────────────────────
Write-Host ""
Write-Host "🗑️  Deleting AKS Arc cluster '$clusterName'..." -ForegroundColor Yellow
Write-Host "   (this may take 5–10 minutes)" -ForegroundColor DarkGray

az aksarc delete `
    --name $clusterName `
    --resource-group $rgName `
    --yes `
    --output none

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to delete AKS Arc cluster '$clusterName'"
    exit 1
}
Write-Host "  ✅ Cluster '$clusterName' deleted" -ForegroundColor Green

# ── Summary ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor Green
Write-Host "│  Cluster deletion complete!                      │" -ForegroundColor Green
Write-Host "│                                                  │" -ForegroundColor Green
Write-Host "│  Deleted:  $clusterName"                            -ForegroundColor Green
Write-Host "│  RG kept:  $rgName"                                 -ForegroundColor Green
Write-Host "│                                                  │" -ForegroundColor Green
Write-Host "│  Next steps:                                     │" -ForegroundColor Green
Write-Host "│    1. scripts/01-create-cluster.ps1              │" -ForegroundColor Green
Write-Host "│    2. scripts/02-install-platform.ps1            │" -ForegroundColor Green
Write-Host "│    3. scripts/06-deploy-video-indexer.ps1        │" -ForegroundColor Green
Write-Host "│       -EnvFile config\vi-portland.env            │" -ForegroundColor Green
Write-Host "│    4. scripts/07-deploy-video-dashboard.ps1      │" -ForegroundColor Green
Write-Host "│       -EnvFile config\vi-portland.env            │" -ForegroundColor Green
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor Green
