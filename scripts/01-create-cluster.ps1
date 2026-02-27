<#
.SYNOPSIS
    Deploy AKS enabled by Azure Arc on Azure Local with system, user, and GPU node pools.

.DESCRIPTION
    Reads config/aks_arc_cluster.env, derives all names from PREFIX, then:
      1. Validates prerequisites (az CLI, extensions, providers)
      2. Retrieves SSH public key from Key Vault
      3. Creates the AKS Arc cluster (system pool)
      4. Adds user node pool
      5. Adds GPU node pool (NVIDIA A2)
      6. Gets kubeconfig and validates node readiness

    Requires: 00-bootstrap-secrets.ps1 to have run first (Key Vault + secrets).

.PARAMETER EnvFile
    Path to the env file. Defaults to config/aks_arc_cluster.env then .env.sample.

.EXAMPLE
    .\scripts\01-create-cluster.ps1
#>

[CmdletBinding()]
param(
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Locate & parse env file ──────────────────────────────────────────────────
if (-not $EnvFile) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\config\aks_arc_cluster.env"),
        (Join-Path $PSScriptRoot "..\config\aks_arc_cluster.env.sample")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $EnvFile = (Resolve-Path $c).Path; break }
    }
}
if (-not $EnvFile -or -not (Test-Path $EnvFile)) {
    Write-Error "Cannot find env file. Copy config/aks_arc_cluster.env.sample → config/aks_arc_cluster.env and fill it in."
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
$prefix          = $envVars["PREFIX"]
if (-not $prefix) { Write-Error "PREFIX is not set in $EnvFile"; exit 1 }

$subscriptionId  = $envVars["SUBSCRIPTION_ID"]
$location        = $envVars["AZURE_METADATA_LOCATION"]
$customLocId     = $envVars["CUSTOM_LOCATION_ID"]
$k8sVersion      = $envVars["KUBERNETES_VERSION"]
$vnetId          = $envVars["VNET_RESOURCE_ID"]
$aadGroupIds     = $envVars["AAD_ADMIN_GROUP_IDS"]

$rgName          = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$clusterName     = if ($envVars["CLUSTER_NAME"])        { $envVars["CLUSTER_NAME"] }        else { "$prefix-aks" }
$kvName          = if ($envVars["KEYVAULT_NAME"])        { $envVars["KEYVAULT_NAME"] }        else { "$prefix-kv" }
$sshKeyName      = if ($envVars["SSH_KEY_NAME"])         { $envVars["SSH_KEY_NAME"] }         else { "$prefix-ssh" }

$sysPoolName     = if ($envVars["SYSTEM_POOL_NAME"])     { $envVars["SYSTEM_POOL_NAME"] }     else { "${prefix}system" }
$sysPoolSize     = $envVars["SYSTEM_POOL_VM_SIZE"]
$sysPoolCount    = $envVars["SYSTEM_POOL_COUNT"]

$userPoolName    = if ($envVars["USER_POOL_NAME"])       { $envVars["USER_POOL_NAME"] }       else { "${prefix}user" }
$userPoolSize    = $envVars["USER_POOL_VM_SIZE"]
$userPoolCount   = $envVars["USER_POOL_COUNT"]

$gpuPoolName     = if ($envVars["GPU_POOL_NAME"])        { $envVars["GPU_POOL_NAME"] }        else { "${prefix}gpu" }
$gpuPoolSize     = $envVars["GPU_POOL_VM_SIZE"]
$gpuPoolCount    = $envVars["GPU_POOL_COUNT"]

# ── Validation ───────────────────────────────────────────────────────────────
$missing = @()
if (-not $subscriptionId) { $missing += "SUBSCRIPTION_ID" }
if (-not $location)       { $missing += "AZURE_METADATA_LOCATION" }
if (-not $customLocId)    { $missing += "CUSTOM_LOCATION_ID" }
if (-not $vnetId)         { $missing += "VNET_RESOURCE_ID" }
if ($missing.Count -gt 0) {
    Write-Error "Missing required env vars: $($missing -join ', '). Fill them in $EnvFile."
    exit 1
}

Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "│  Prefix:           $prefix"                         -ForegroundColor DarkCyan
Write-Host "│  Subscription:     $subscriptionId"                 -ForegroundColor DarkCyan
Write-Host "│  Location:         $location"                       -ForegroundColor DarkCyan
Write-Host "│  Resource Group:   $rgName"                         -ForegroundColor DarkCyan
Write-Host "│  Cluster:          $clusterName"                    -ForegroundColor DarkCyan
Write-Host "│  K8s Version:      $k8sVersion"                     -ForegroundColor DarkCyan
Write-Host "│  Custom Location:  $customLocId"                    -ForegroundColor DarkCyan
Write-Host "│  VNet:             $vnetId"                         -ForegroundColor DarkCyan
Write-Host "│  System pool:      $sysPoolCount × $sysPoolSize"    -ForegroundColor DarkCyan
Write-Host "│  User pool:        $userPoolCount × $userPoolSize"  -ForegroundColor DarkCyan
Write-Host "│  GPU pool:         $gpuPoolCount × $gpuPoolSize"    -ForegroundColor DarkCyan
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor DarkCyan
Write-Host ""

# ── Step 0: Prerequisites ───────────────────────────────────────────────────
Write-Host "🔍 Checking prerequisites..." -ForegroundColor Yellow

# Ensure az CLI is available
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    Write-Error "az CLI not found. Install from https://aka.ms/installazurecli"
    exit 1
}

# Ensure required extensions
$requiredExtensions = @("connectedk8s", "k8s-extension", "k8s-configuration", "aksarc")
foreach ($ext in $requiredExtensions) {
    $installed = az extension show --name $ext --query "name" -o tsv 2>$null
    if (-not $installed) {
        Write-Host "  📦 Installing az extension: $ext" -ForegroundColor Yellow
        az extension add --name $ext --upgrade --yes 2>$null
    } else {
        Write-Host "  ✅ Extension '$ext' installed" -ForegroundColor Green
    }
}

# Ensure required providers are registered
$requiredProviders = @(
    "Microsoft.Kubernetes",
    "Microsoft.KubernetesConfiguration",
    "Microsoft.ExtendedLocation",
    "Microsoft.HybridContainerService"
)
foreach ($p in $requiredProviders) {
    $state = az provider show -n $p --query "registrationState" -o tsv 2>$null
    if ($state -ne "Registered") {
        Write-Host "  📝 Registering provider: $p" -ForegroundColor Yellow
        az provider register -n $p 2>$null
    } else {
        Write-Host "  ✅ Provider '$p' registered" -ForegroundColor Green
    }
}

Write-Host ""

# ── Step 1: Set subscription ────────────────────────────────────────────────
Write-Host "🔑 Setting subscription..." -ForegroundColor Yellow
az account set --subscription $subscriptionId
if ($LASTEXITCODE -ne 0) { Write-Error "az account set failed"; exit 1 }

# ── Step 2: Ensure resource group exists ────────────────────────────────────
$rgExists = az group exists --name $rgName 2>$null
if ($rgExists -ne "true") {
    Write-Host "🔨 Creating resource group '$rgName'..." -ForegroundColor Yellow
    az group create --name $rgName --location $location --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create resource group"; exit 1 }
}
Write-Host "✅ Resource group: $rgName" -ForegroundColor Green

# ── Step 3: Retrieve SSH public key from Key Vault ──────────────────────────
Write-Host "🔐 Retrieving SSH public key from Key Vault '$kvName'..." -ForegroundColor Yellow
$sshPubKey = az keyvault secret show --vault-name $kvName --name "$sshKeyName-pub" --query "value" -o tsv 2>$null
if (-not $sshPubKey) {
    Write-Error "SSH public key not found in Key Vault. Run 00-bootstrap-secrets.ps1 first."
    exit 1
}
Write-Host "  ✅ SSH public key retrieved" -ForegroundColor Green

# ── Step 4: Create AKS Arc cluster ─────────────────────────────────────────
Write-Host ""
Write-Host "🚀 Creating AKS Arc cluster '$clusterName'..." -ForegroundColor Yellow
Write-Host "   (this may take 10–20 minutes)" -ForegroundColor DarkGray

$createArgs = @(
    "aksarc", "create",
    "--name", $clusterName,
    "--resource-group", $rgName,
    "--custom-location", $customLocId,
    "--vnet-ids", $vnetId,
    "--kubernetes-version", $k8sVersion,
    "--ssh-key-value", $sshPubKey,
    "--node-count", $sysPoolCount,
    "--node-vm-size", $sysPoolSize,
    "--generate-ssh-keys",
    "--output", "none"
)

# Add AAD admin group IDs if specified
if ($aadGroupIds) {
    $createArgs += @("--aad-admin-group-object-ids", $aadGroupIds)
}

$clusterExists = az aksarc show --name $clusterName --resource-group $rgName --query "name" -o tsv 2>$null
if ($clusterExists) {
    Write-Host "  ⏭️  Cluster '$clusterName' already exists – skipping create" -ForegroundColor DarkGray
} else {
    az @createArgs
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create AKS Arc cluster"; exit 1 }
    Write-Host "  ✅ Cluster created" -ForegroundColor Green
}

# ── Step 5: Add user node pool ──────────────────────────────────────────────
Write-Host ""
Write-Host "➕ Adding user node pool '$userPoolName'..." -ForegroundColor Yellow

$userPoolExists = az aksarc nodepool show --cluster-name $clusterName --resource-group $rgName --name $userPoolName --query "name" -o tsv 2>$null
if ($userPoolExists) {
    Write-Host "  ⏭️  Node pool '$userPoolName' already exists – skipping" -ForegroundColor DarkGray
} else {
    az aksarc nodepool add `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --name $userPoolName `
        --node-count $userPoolCount `
        --node-vm-size $userPoolSize `
        --os-type Linux `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to add user node pool"; exit 1 }
    Write-Host "  ✅ User node pool added" -ForegroundColor Green
}

# ── Step 6: Add GPU node pool ───────────────────────────────────────────────
Write-Host ""
Write-Host "➕ Adding GPU node pool '$gpuPoolName' ($gpuPoolSize)..." -ForegroundColor Yellow

$gpuPoolExists = az aksarc nodepool show --cluster-name $clusterName --resource-group $rgName --name $gpuPoolName --query "name" -o tsv 2>$null
if ($gpuPoolExists) {
    Write-Host "  ⏭️  Node pool '$gpuPoolName' already exists – skipping" -ForegroundColor DarkGray
} else {
    az aksarc nodepool add `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --name $gpuPoolName `
        --node-count $gpuPoolCount `
        --node-vm-size $gpuPoolSize `
        --os-type Linux `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to add GPU node pool"; exit 1 }
    Write-Host "  ✅ GPU node pool added" -ForegroundColor Green
}

# ── Step 7: Get kubeconfig ──────────────────────────────────────────────────
Write-Host ""
Write-Host "📝 Retrieving kubeconfig..." -ForegroundColor Yellow

az aksarc get-credentials `
    --name $clusterName `
    --resource-group $rgName `
    --overwrite-existing `
    --output none 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ⚠️  Could not retrieve kubeconfig automatically. You may need to get it manually." -ForegroundColor DarkYellow
} else {
    Write-Host "  ✅ kubeconfig merged into ~/.kube/config" -ForegroundColor Green
}

# ── Step 8: Validate ────────────────────────────────────────────────────────
Write-Host ""
Write-Host "🔍 Validating cluster..." -ForegroundColor Yellow

$nodes = kubectl get nodes -o wide 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host $nodes
    Write-Host ""

    # Check for GPU
    $gpuInfo = kubectl describe nodes | Select-String "nvidia.com/gpu" 2>$null
    if ($gpuInfo) {
        Write-Host "  ✅ GPU resources detected:" -ForegroundColor Green
        $gpuInfo | ForEach-Object { Write-Host "     $_" -ForegroundColor Green }
    } else {
        Write-Host "  ⚠️  No GPU resources detected yet (NVIDIA device plugin may still be initializing)" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "  ⚠️  Cannot reach cluster via kubectl yet. Verify kubeconfig and connectivity." -ForegroundColor DarkYellow
}

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor Green
Write-Host "│  Cluster deployment complete!                    │" -ForegroundColor Green
Write-Host "│                                                  │" -ForegroundColor Green
Write-Host "│  Cluster:      $clusterName"                        -ForegroundColor Green
Write-Host "│  RG:           $rgName"                             -ForegroundColor Green
Write-Host "│  Node pools:   $sysPoolName, $userPoolName, $gpuPoolName" -ForegroundColor Green
Write-Host "│                                                  │" -ForegroundColor Green
Write-Host "│  Next: run scripts/02-install-platform.ps1       │" -ForegroundColor Green
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor Green
