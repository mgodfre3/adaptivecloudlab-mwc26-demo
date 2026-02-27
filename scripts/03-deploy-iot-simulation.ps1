<#
.SYNOPSIS
    Deploy IoT Hub and register simulated drone devices for the MWC demo.

.DESCRIPTION
    1. Deploys an Azure IoT Hub via Bicep (iot-simulation/iot-hub-deployment.bicep)
    2. Registers N drone device identities (N = DRONE_COUNT from env)
    3. Stores each device connection string in Azure Key Vault
    4. Generates a .env file for the drone telemetry simulator

    Requires:
      - 02-install-platform.ps1 to have run (IoT Ops on cluster)
      - az CLI logged in with Contributor on the resource group

.PARAMETER EnvFile
    Path to the env file.  Defaults to config/aks_arc_cluster.env then .env.sample.

.PARAMETER SkipHubDeploy
    Skip IoT Hub Bicep deployment (hub already exists).

.PARAMETER SkipDevices
    Skip device identity creation.

.EXAMPLE
    .\scripts\03-deploy-iot-simulation.ps1
    .\scripts\03-deploy-iot-simulation.ps1 -SkipHubDeploy
#>

[CmdletBinding()]
param(
    [string]$EnvFile,
    [switch]$SkipHubDeploy,
    [switch]$SkipDevices
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

$azCmd = if ($IsWindows -or $env:OS -match 'Windows') {
    (Get-Command az.cmd -ErrorAction SilentlyContinue)?.Source ?? 'az'
} else { 'az' }

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

# ── Derive names ─────────────────────────────────────────────────────────────
$prefix       = $envVars["PREFIX"]
if (-not $prefix) { Write-Error "PREFIX is not set in $EnvFile"; exit 1 }

$location     = $envVars["AZURE_METADATA_LOCATION"]
$rgName       = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$kvName       = if ($envVars["KEYVAULT_NAME"])       { $envVars["KEYVAULT_NAME"] }       else { "$prefix-kv" }
$droneCount   = if ($envVars["DRONE_COUNT"])         { [int]$envVars["DRONE_COUNT"] }    else { 5 }
$iotHubName   = "$prefix-iothub"

$bicepFile    = Join-Path $PSScriptRoot "..\iot-simulation\iot-hub-deployment.bicep"
$simEnvFile   = Join-Path $PSScriptRoot "..\iot-simulation\.env"

Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "│  IoT Simulation Deployment Plan                  │" -ForegroundColor DarkCyan
Write-Host "│──────────────────────────────────────────────────│" -ForegroundColor DarkCyan
Write-Host "│  IoT Hub:        $iotHubName"                       -ForegroundColor DarkCyan
Write-Host "│  Resource Group:  $rgName"                          -ForegroundColor DarkCyan
Write-Host "│  Location:        $location"                        -ForegroundColor DarkCyan
Write-Host "│  Drones:          $droneCount"                      -ForegroundColor DarkCyan
Write-Host "│  Key Vault:       $kvName"                          -ForegroundColor DarkCyan
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor DarkCyan
Write-Host ""

# ── Step 1: Deploy IoT Hub via Bicep ─────────────────────────────────────────
if (-not $SkipHubDeploy) {
    Write-Host "━━━ Step 1: Deploy IoT Hub ━━━" -ForegroundColor Magenta

    if (-not (Test-Path $bicepFile)) {
        Write-Error "Bicep template not found: $bicepFile"
        exit 1
    }

    # Check if hub already exists
    $hubExists = & $azCmd iot hub show --name $iotHubName -g $rgName --query "name" -o tsv 2>$null
    if ($hubExists) {
        Write-Host "  ✅ IoT Hub '$iotHubName' already exists" -ForegroundColor Green
    } else {
        Write-Host "  📦 Deploying IoT Hub '$iotHubName'..." -ForegroundColor Yellow

        & $azCmd deployment group create `
            -g $rgName `
            --template-file $bicepFile `
            --parameters prefix=$prefix location=$location droneCount=$droneCount `
            --name "iot-hub-$(Get-Date -Format 'yyyyMMdd-HHmmss')" `
            --no-prompt 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Error "IoT Hub deployment failed."
            exit 1
        }
        Write-Host "  ✅ IoT Hub '$iotHubName' deployed" -ForegroundColor Green
    }
} else {
    Write-Host "⏭️  Skipping IoT Hub deployment (--SkipHubDeploy)" -ForegroundColor DarkGray
}

# ── Step 2: Register drone device identities ─────────────────────────────────
if (-not $SkipDevices) {
    Write-Host ""
    Write-Host "━━━ Step 2: Register Drone Devices ━━━" -ForegroundColor Magenta

    # Ensure the azure-iot extension is available
    & $azCmd extension add --name azure-iot --upgrade --yes 2>$null

    $connectionStrings = @{}

    for ($i = 1; $i -le $droneCount; $i++) {
        $deviceId = "drone-$i"

        # Check if device already exists
        $deviceExists = & $azCmd iot hub device-identity show `
            --hub-name $iotHubName `
            --device-id $deviceId `
            --query "deviceId" -o tsv 2>$null

        if ($deviceExists) {
            Write-Host "  ✅ Device '$deviceId' already registered" -ForegroundColor Green
        } else {
            Write-Host "  📦 Creating device '$deviceId'..." -ForegroundColor Yellow
            & $azCmd iot hub device-identity create `
                --hub-name $iotHubName `
                --device-id $deviceId `
                --edge-enabled false 2>&1

            if ($LASTEXITCODE -ne 0) {
                Write-Warning "⚠️  Failed to create device '$deviceId'"
                continue
            }
            Write-Host "  ✅ Device '$deviceId' registered" -ForegroundColor Green
        }

        # Get connection string
        $connStr = & $azCmd iot hub device-identity connection-string show `
            --hub-name $iotHubName `
            --device-id $deviceId `
            --query "connectionString" -o tsv
        $connectionStrings[$deviceId] = $connStr

        # Store in Key Vault
        $secretName = "$prefix-$deviceId-connstr"
        & $azCmd keyvault secret set `
            --vault-name $kvName `
            --name $secretName `
            --value $connStr `
            --content-type "IoT Hub device connection string" `
            -o none 2>$null
        Write-Host "     Secret '$secretName' saved to Key Vault" -ForegroundColor DarkGray
    }

    # ── Step 3: Generate .env file for the simulator ─────────────────────────
    Write-Host ""
    Write-Host "━━━ Step 3: Generate Simulator .env File ━━━" -ForegroundColor Magenta

    $envContent = @"
# Auto-generated by 03-deploy-iot-simulation.ps1 — $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
# Drone Telemetry Simulator Configuration
IOT_HUB_NAME=$iotHubName
DRONE_COUNT=$droneCount
SEND_INTERVAL_SECONDS=5

"@
    for ($i = 1; $i -le $droneCount; $i++) {
        $deviceId = "drone-$i"
        $connStr = $connectionStrings[$deviceId]
        if ($connStr) {
            $envContent += "DRONE_${i}_CONNECTION_STRING=$connStr`n"
        }
    }

    Set-Content -Path $simEnvFile -Value $envContent -NoNewline
    Write-Host "  ✅ Simulator .env written to: $simEnvFile" -ForegroundColor Green
    Write-Host "     Contains connection strings for $droneCount drones" -ForegroundColor DarkGray
} else {
    Write-Host "⏭️  Skipping device registration (--SkipDevices)" -ForegroundColor DarkGray
}

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor Green
Write-Host "│  IoT Simulation Deployment Complete              │" -ForegroundColor Green
Write-Host "└──────────────────────────────────────────────────┘" -ForegroundColor Green
Write-Host ""
Write-Host "To run the drone telemetry simulator:" -ForegroundColor Cyan
Write-Host "  cd iot-simulation" -ForegroundColor Cyan
Write-Host "  pip install -r requirements.txt" -ForegroundColor Cyan
Write-Host "  python drone-telemetry-simulator.py" -ForegroundColor Cyan
Write-Host ""
