<#
.SYNOPSIS
    Bootstrap script: creates the Resource Group, Key Vault, SSH key pair,
    and all auto-generated passwords/secrets for the Drone Network Monitoring demo.

.DESCRIPTION
    Reads config/aks_arc_cluster.env (or .env.sample), derives names from PREFIX,
    then creates:
      - Resource Group  (${PREFIX}-rg)
      - Key Vault       (${PREFIX}-kv)
    And generates + stores:
      - SSH key pair     → KV secret ${PREFIX}-ssh       (private key)
                         → KV secret ${PREFIX}-ssh-pub   (public key)
      - MQTT password    → KV secret ${PREFIX}-mqtt-pwd
      - Foundry API key  → KV secret ${PREFIX}-foundry-key
      - Admin password   → KV secret ${PREFIX}-admin-pwd

    Re-running is safe: existing secrets are skipped (not overwritten).

.PARAMETER EnvFile
    Path to the env file. Defaults to config/aks_arc_cluster.env then .env.sample.

.EXAMPLE
    .\scripts\00-bootstrap-secrets.ps1
    .\scripts\00-bootstrap-secrets.ps1 -EnvFile config/aks_arc_cluster.env
#>

[CmdletBinding()]
param(
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Locate & parse env file ──────────────────────────────────────────────────
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $repoRoot) { $repoRoot = Split-Path -Parent $PSScriptRoot }

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
    Write-Error "Cannot find env file. Copy config/aks_arc_cluster.env.sample to config/aks_arc_cluster.env and fill it in."
    exit 1
}

Write-Host "📄 Loading env from: $EnvFile" -ForegroundColor Cyan

# Parse KEY=VALUE lines (ignore comments and blank lines)
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
$prefix = $envVars["PREFIX"]
if (-not $prefix) { Write-Error "PREFIX is not set in $EnvFile"; exit 1 }

$subscriptionId      = $envVars["SUBSCRIPTION_ID"]
$location            = $envVars["AZURE_METADATA_LOCATION"]
$rgName              = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$kvName              = if ($envVars["KEYVAULT_NAME"])       { $envVars["KEYVAULT_NAME"] }       else { "$prefix-kv" }
$sshKeyName          = if ($envVars["SSH_KEY_NAME"])        { $envVars["SSH_KEY_NAME"] }        else { "$prefix-ssh" }
$mqttPwdName         = if ($envVars["MQTT_PASSWORD_NAME"])  { $envVars["MQTT_PASSWORD_NAME"] }  else { "$prefix-mqtt-pwd" }
$foundryKeyName      = if ($envVars["FOUNDRY_API_KEY_NAME"]){ $envVars["FOUNDRY_API_KEY_NAME"]} else { "$prefix-foundry-key" }
$adminPwdName        = if ($envVars["ADMIN_PASSWORD_NAME"]) { $envVars["ADMIN_PASSWORD_NAME"] } else { "$prefix-admin-pwd" }

if (-not $subscriptionId) { Write-Error "SUBSCRIPTION_ID is not set in $EnvFile"; exit 1 }
if (-not $location)       { Write-Error "AZURE_METADATA_LOCATION is not set in $EnvFile"; exit 1 }

Write-Host ""
Write-Host "┌─────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "│  Prefix:          $prefix"                      -ForegroundColor DarkCyan
Write-Host "│  Subscription:    $subscriptionId"              -ForegroundColor DarkCyan
Write-Host "│  Location:        $location"                    -ForegroundColor DarkCyan
Write-Host "│  Resource Group:  $rgName"                      -ForegroundColor DarkCyan
Write-Host "│  Key Vault:       $kvName"                      -ForegroundColor DarkCyan
Write-Host "└─────────────────────────────────────────────┘" -ForegroundColor DarkCyan
Write-Host ""

# ── Set subscription ─────────────────────────────────────────────────────────
Write-Host "🔑 Setting subscription..." -ForegroundColor Yellow
az account set --subscription $subscriptionId
if ($LASTEXITCODE -ne 0) { Write-Error "az account set failed"; exit 1 }

# ── Create resource group ────────────────────────────────────────────────────
$rgExists = az group exists --name $rgName 2>$null
if ($rgExists -eq "true") {
    Write-Host "✅ Resource group '$rgName' already exists" -ForegroundColor Green
} else {
    Write-Host "🔨 Creating resource group '$rgName' in '$location'..." -ForegroundColor Yellow
    az group create --name $rgName --location $location --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create resource group"; exit 1 }
    Write-Host "✅ Resource group created" -ForegroundColor Green
}

# ── Create Key Vault ─────────────────────────────────────────────────────────
$kvExists = az keyvault show --name $kvName --resource-group $rgName --query "name" -o tsv 2>$null
if ($kvExists) {
    Write-Host "✅ Key Vault '$kvName' already exists" -ForegroundColor Green
} else {
    Write-Host "🔨 Creating Key Vault '$kvName'..." -ForegroundColor Yellow
    az keyvault create `
        --name $kvName `
        --resource-group $rgName `
        --location $location `
        --enable-rbac-authorization false `
        --sku standard `
        --output none
    if ($LASTEXITCODE -ne 0) { Write-Error "Failed to create Key Vault"; exit 1 }
    Write-Host "✅ Key Vault created" -ForegroundColor Green
}

# ── Helper: store secret if it doesn't already exist ─────────────────────────
function Set-KVSecretIfMissing {
    param(
        [string]$VaultName,
        [string]$SecretName,
        [string]$SecretValue,
        [string]$Description
    )
    $existing = az keyvault secret show --vault-name $VaultName --name $SecretName --query "value" -o tsv 2>$null
    if ($existing) {
        Write-Host "  ⏭️  $Description ('$SecretName') already exists – skipping" -ForegroundColor DarkGray
    } else {
        az keyvault secret set --vault-name $VaultName --name $SecretName --value $SecretValue --output none 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Error "Failed to store secret '$SecretName'"; exit 1 }
        Write-Host "  ✅ $Description ('$SecretName') stored" -ForegroundColor Green
    }
}

# ── Helper: generate a strong random password ────────────────────────────────
function New-StrongPassword {
    param([int]$Length = 32)
    # Avoid characters that PowerShell or az CLI interpret: ! " ' ` $ { } ( )
    $chars = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@#%-_=+.~'
    $bytes = [byte[]]::new($Length)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $pw = -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
    return $pw
}

# ── Generate & store SSH key pair ────────────────────────────────────────────
Write-Host ""
Write-Host "🔐 SSH Key Pair" -ForegroundColor Yellow

$sshDir = Join-Path ([System.IO.Path]::GetTempPath()) "drone-demo-ssh-$prefix"
$sshPrivPath = Join-Path $sshDir "id_rsa"
$sshPubPath  = "$sshPrivPath.pub"

$existingPriv = az keyvault secret show --vault-name $kvName --name $sshKeyName --query "value" -o tsv 2>$null
if ($existingPriv) {
    Write-Host "  ⏭️  SSH private key ('$sshKeyName') already exists – skipping" -ForegroundColor DarkGray
} else {
    # Generate fresh key pair
    if (Test-Path $sshDir) { Remove-Item $sshDir -Recurse -Force }
    New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
    ssh-keygen -t rsa -b 4096 -f $sshPrivPath -N '""' -q
    if (-not (Test-Path $sshPrivPath)) { Write-Error "ssh-keygen failed"; exit 1 }

    $privKey = Get-Content $sshPrivPath -Raw
    $pubKey  = Get-Content $sshPubPath  -Raw

    # Store both in KV
    az keyvault secret set --vault-name $kvName --name $sshKeyName       --value $privKey --output none 2>$null
    az keyvault secret set --vault-name $kvName --name "$sshKeyName-pub" --value $pubKey  --output none 2>$null
    Write-Host "  ✅ SSH key pair stored ('$sshKeyName', '$sshKeyName-pub')" -ForegroundColor Green

    # Clean up temp files
    Remove-Item $sshDir -Recurse -Force
}

# ── Generate & store passwords ───────────────────────────────────────────────
Write-Host ""
Write-Host "🔐 Passwords & API Keys" -ForegroundColor Yellow

Set-KVSecretIfMissing -VaultName $kvName -SecretName $mqttPwdName    -SecretValue (New-StrongPassword 32) -Description "MQTT password"
Set-KVSecretIfMissing -VaultName $kvName -SecretName $foundryKeyName -SecretValue (New-StrongPassword 48) -Description "Foundry API key"
Set-KVSecretIfMissing -VaultName $kvName -SecretName $adminPwdName   -SecretValue (New-StrongPassword 24) -Description "Admin password"

# ── Summary ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌─────────────────────────────────────────────┐" -ForegroundColor Green
Write-Host "│  Bootstrap complete!                        │" -ForegroundColor Green
Write-Host "│                                             │" -ForegroundColor Green
Write-Host "│  Key Vault: $kvName"                           -ForegroundColor Green
Write-Host "│  Secrets:                                   │" -ForegroundColor Green
Write-Host "│    $sshKeyName         (SSH private key)"      -ForegroundColor Green
Write-Host "│    $sshKeyName-pub     (SSH public key)"       -ForegroundColor Green
Write-Host "│    $mqttPwdName        (MQTT password)"        -ForegroundColor Green
Write-Host "│    $foundryKeyName     (Foundry API key)"      -ForegroundColor Green
Write-Host "│    $adminPwdName       (Admin password)"       -ForegroundColor Green
Write-Host "└─────────────────────────────────────────────┘" -ForegroundColor Green
Write-Host ""
Write-Host "Next: run scripts/01-create-cluster.ps1" -ForegroundColor Cyan
