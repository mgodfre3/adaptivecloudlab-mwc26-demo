<#
.SYNOPSIS
    Deploy Arc Video Indexer extension + Longhorn storage on AKS Arc (Mobile stamp).

.DESCRIPTION
    Reads config/vi-mobile.env, derives all names from PREFIX, then installs:
      1. Longhorn (Helm) for RWX storage class
      2. Arc Video Indexer extension (az k8s-extension)

    Requires:
      - 01-create-cluster.ps1 to have run (cluster exists with node pools)
      - 02-install-platform.ps1 to have run (NGINX Ingress, cert-manager, etc.)
      - kubectl current context pointing at the cluster
      - Helm 3 installed

.PARAMETER EnvFile
    Path to the env file. Defaults to config/vi-mobile.env.

.PARAMETER SkipLonghorn
    Skip Longhorn storage installation.

.PARAMETER SkipVideoIndexer
    Skip Arc Video Indexer extension installation.

.EXAMPLE
    .\scripts\06-deploy-video-indexer.ps1
    .\scripts\06-deploy-video-indexer.ps1 -SkipLonghorn
#>

[CmdletBinding()]
param(
    [string]$EnvFile,
    [switch]$SkipLonghorn,
    [switch]$SkipVideoIndexer
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── az CLI workaround ────────────────────────────────────────────────────────
if (-not $env:AZURE_EXTENSION_DIR) {
    $env:AZURE_EXTENSION_DIR = Join-Path $env:TEMP "az_extensions"
}
if (-not (Test-Path $env:AZURE_EXTENSION_DIR)) {
    New-Item -ItemType Directory -Path $env:AZURE_EXTENSION_DIR -Force | Out-Null
}
Write-Host "🔧 AZURE_EXTENSION_DIR = $env:AZURE_EXTENSION_DIR" -ForegroundColor DarkGray

# Helper: resolve 'az.cmd' on Windows to avoid PowerShell alias conflicts
$azCmd = if ($IsWindows -or $env:OS -match 'Windows') {
    (Get-Command az.cmd -ErrorAction SilentlyContinue)?.Source ?? 'az'
} else { 'az' }

# ── Locate & parse env file ──────────────────────────────────────────────────
if (-not $EnvFile) {
    $candidates = @(
        (Join-Path $PSScriptRoot "..\config\vi-mobile.env")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $EnvFile = (Resolve-Path $c).Path; break }
    }
}
if (-not $EnvFile -or -not (Test-Path $EnvFile)) {
    Write-Error "Cannot find env file. Ensure config/vi-mobile.env exists."
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
$rgName          = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$clusterName     = if ($envVars["CLUSTER_NAME"])        { $envVars["CLUSTER_NAME"] }        else { "$prefix-aks" }

# Video Indexer settings
$viExtName       = if ($envVars["VI_EXTENSION_NAME"])   { $envVars["VI_EXTENSION_NAME"] }   else { "videoindexer" }
$viStorageClass  = if ($envVars["VI_STORAGE_CLASS"])     { $envVars["VI_STORAGE_CLASS"] }     else { "longhorn" }

# Longhorn values file
$longhornValues  = Join-Path (Split-Path $PSScriptRoot) "k8s\longhorn-values.yaml"

# ── Display plan ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "│  Video Indexer Deployment Plan                   │" -ForegroundColor DarkCyan
Write-Host "│──────────────────────────────────────────────────│" -ForegroundColor DarkCyan
Write-Host "│  Prefix:             $prefix"                       -ForegroundColor DarkCyan
Write-Host "│  Cluster:            $clusterName"                  -ForegroundColor DarkCyan
Write-Host "│  Resource Group:     $rgName"                       -ForegroundColor DarkCyan
Write-Host "│  Location:           $location"                     -ForegroundColor DarkCyan
Write-Host "│  Storage Class:      $viStorageClass"               -ForegroundColor DarkCyan
Write-Host "│  VI Extension:       $viExtName"                    -ForegroundColor DarkCyan
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor DarkCyan
Write-Host ""

# ── Step 0: Prerequisites ───────────────────────────────────────────────────
Write-Host "🔍 Checking prerequisites..." -ForegroundColor Yellow

foreach ($tool in @("kubectl", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Error "$tool not found. Install it before running this script."
        exit 1
    }
}
Write-Host "  ✅ kubectl and helm found" -ForegroundColor Green

# Verify kubectl connectivity
$nodeCheck = kubectl get nodes --no-headers 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Error "kubectl cannot reach the cluster. Ensure 'az connectedk8s proxy' is running or kubeconfig is set."
    exit 1
}
$nodeCount = ($nodeCheck | Measure-Object -Line).Lines
Write-Host "  ✅ Cluster reachable ($nodeCount nodes)" -ForegroundColor Green

# ── Step 1: Longhorn Storage ────────────────────────────────────────────────
if (-not $SkipLonghorn) {
    Write-Host ""
    Write-Host "━━━ Step 1: Longhorn Storage (RWX) ━━━" -ForegroundColor Magenta

    # Check if already installed
    $longhornDeployment = kubectl get deployment longhorn-driver-deployer -n longhorn-system -o name 2>$null
    if ($longhornDeployment) {
        Write-Host "  ✅ Longhorn already installed in ns/longhorn-system" -ForegroundColor Green
    } else {
        Write-Host "  📦 Adding longhorn Helm repo..." -ForegroundColor Yellow
        helm repo add longhorn https://charts.longhorn.io 2>$null
        helm repo update longhorn 2>$null

        Write-Host "  📦 Installing Longhorn..." -ForegroundColor Yellow
        $helmArgs = @(
            "install", "longhorn", "longhorn/longhorn",
            "--namespace", "longhorn-system",
            "--create-namespace",
            "--set", "defaultSettings.defaultDataPath=/var/lib/longhorn",
            "--wait", "--timeout", "10m"
        )

        # Use values file if it exists
        if (Test-Path $longhornValues) {
            $helmArgs += @("--values", $longhornValues)
            Write-Host "      Values: $longhornValues" -ForegroundColor DarkGray
        }

        helm @helmArgs 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  Longhorn install returned non-zero exit code. Check 'helm status longhorn -n longhorn-system'."
        } else {
            Write-Host "  ✅ Longhorn installed" -ForegroundColor Green
        }
    }

    # Verify storage class exists
    Write-Host "  🔍 Verifying Longhorn storage class..." -ForegroundColor Yellow
    $scExists = kubectl get storageclass longhorn -o name 2>$null
    if ($scExists) {
        Write-Host "  ✅ Storage class 'longhorn' available" -ForegroundColor Green
    } else {
        Write-Host "  ⚠️  Storage class 'longhorn' not found — Longhorn may still be initializing" -ForegroundColor Yellow
        Write-Host "      Check: kubectl get storageclass" -ForegroundColor DarkGray
    }
} else {
    Write-Host "⏭️  Skipping Longhorn (--SkipLonghorn)" -ForegroundColor DarkGray
}

# ── Step 2: Arc Video Indexer Extension ──────────────────────────────────────
if (-not $SkipVideoIndexer) {
    Write-Host ""
    Write-Host "━━━ Step 2: Arc Video Indexer Extension ━━━" -ForegroundColor Magenta

    # Ensure required az CLI extensions
    Write-Host "  🔧 Ensuring az CLI extensions..." -ForegroundColor Yellow
    foreach ($ext in @("k8s-extension", "connectedk8s")) {
        & $azCmd extension add --name $ext --upgrade --yes 2>$null
    }
    Write-Host "  ✅ az CLI extensions ready" -ForegroundColor Green

    # Check if already installed
    $viExtState = & $azCmd k8s-extension show `
        --name $viExtName `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --cluster-type connectedClusters `
        --query "provisioningState" -o tsv 2>$null

    if ($viExtState -eq "Succeeded") {
        Write-Host "  ✅ Video Indexer extension '$viExtName' already deployed ($viExtState)" -ForegroundColor Green
    } else {
        Write-Host "  📦 Deploying Arc Video Indexer extension..." -ForegroundColor Yellow
        Write-Host "      Extension:      $viExtName" -ForegroundColor DarkGray
        Write-Host "      Storage Class:  $viStorageClass" -ForegroundColor DarkGray
        Write-Host "      Access Mode:    ReadWriteMany" -ForegroundColor DarkGray

        & $azCmd k8s-extension create `
            --name $viExtName `
            --cluster-name $clusterName `
            --resource-group $rgName `
            --cluster-type connectedClusters `
            --extension-type Microsoft.VideoIndexer `
            --configuration-settings "storage.storageClass=$viStorageClass" `
            --configuration-settings "storage.accessMode=ReadWriteMany" 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  Video Indexer extension install returned non-zero exit code."
            Write-Warning "    Check: & $azCmd k8s-extension show --name $viExtName --cluster-name $clusterName -g $rgName --cluster-type connectedClusters"
        } else {
            Write-Host "  ✅ Video Indexer extension '$viExtName' created" -ForegroundColor Green
        }

        # Wait for extension to be ready
        Write-Host "  ⏳ Waiting for extension to become ready..." -ForegroundColor Yellow
        $maxRetries = 30
        $retryDelay = 20
        for ($i = 1; $i -le $maxRetries; $i++) {
            $extState = & $azCmd k8s-extension show `
                --name $viExtName `
                --cluster-name $clusterName `
                --resource-group $rgName `
                --cluster-type connectedClusters `
                --query "provisioningState" -o tsv 2>$null

            if ($extState -eq "Succeeded") {
                Write-Host "  ✅ Extension is ready ($extState)" -ForegroundColor Green
                break
            } elseif ($extState -eq "Failed") {
                Write-Host "  ❌ Extension provisioning failed" -ForegroundColor Red
                break
            }

            Write-Host "      Attempt $i/$maxRetries — state: $extState (retrying in ${retryDelay}s)" -ForegroundColor DarkGray
            Start-Sleep -Seconds $retryDelay
        }
    }
} else {
    Write-Host "⏭️  Skipping Video Indexer (--SkipVideoIndexer)" -ForegroundColor DarkGray
}

# ── Verification ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "━━━ Verification ━━━" -ForegroundColor Magenta
Write-Host ""

$allGood = $true

# Check Longhorn
if (-not $SkipLonghorn) {
    $lhReady = kubectl get deployment longhorn-driver-deployer -n longhorn-system -o jsonpath='{.status.readyReplicas}' 2>$null
    if ($lhReady -and [int]$lhReady -ge 1) {
        Write-Host "  ✅ Longhorn:              ready ($lhReady replicas)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Longhorn:              not ready" -ForegroundColor Red
        $allGood = $false
    }

    $scCheck = kubectl get storageclass longhorn -o name 2>$null
    if ($scCheck) {
        Write-Host "  ✅ StorageClass longhorn:  available" -ForegroundColor Green
    } else {
        Write-Host "  ❌ StorageClass longhorn:  not found" -ForegroundColor Red
        $allGood = $false
    }
}

# Check Video Indexer extension
if (-not $SkipVideoIndexer) {
    $viState = & $azCmd k8s-extension show `
        --name $viExtName `
        --cluster-name $clusterName `
        --resource-group $rgName `
        --cluster-type connectedClusters `
        --query "provisioningState" -o tsv 2>$null
    if ($viState -eq "Succeeded") {
        Write-Host "  ✅ Video Indexer ext:      $viState" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Video Indexer ext:      $viState" -ForegroundColor Red
        $allGood = $false
    }
}

Write-Host ""
if ($allGood) {
    Write-Host "🎉 Video Indexer infrastructure is healthy!" -ForegroundColor Green
} else {
    Write-Host "⚠️  Some components are not ready. Review warnings above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Video Indexer deployment complete. Next steps:" -ForegroundColor Cyan
Write-Host "  1. Deploy Foundry Local model:   kubectl apply -f k8s/vi-foundry-local.yaml" -ForegroundColor Cyan
Write-Host "  2. Deploy video dashboard:       .\scripts\07-deploy-video-dashboard.ps1" -ForegroundColor Cyan
Write-Host "  3. Deploy monitoring:            .\scripts\05-deploy-monitoring.ps1 -EnvFile config\vi-mobile.env" -ForegroundColor Cyan
Write-Host ""
