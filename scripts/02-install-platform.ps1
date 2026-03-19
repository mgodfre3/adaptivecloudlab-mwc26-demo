<#
.SYNOPSIS
    Install platform components on AKS Arc cluster for the Drone Network Monitoring demo.

.DESCRIPTION
    Reads config/aks_arc_cluster.env, derives all names from PREFIX, then installs:
      1. NGINX Ingress Controller (via Helm)
      2. cert-manager v1.19.2 (via Helm)
      3. trust-manager v0.20.3 (via Helm)
      4. Foundry Local Inference Operator (via Helm OCI chart)
      5. Azure IoT Operations (via az iot ops init)

    Requires:
      - 01-create-cluster.ps1 to have run (cluster exists with node pools)
      - kubectl current context pointing at the cluster
        (e.g. 'az connectedk8s proxy --name <cluster> --resource-group <rg>')
      - Helm 3 installed

.PARAMETER EnvFile
    Path to the env file. Defaults to config/aks_arc_cluster.env then .env.sample.

.PARAMETER SkipIngress
    Skip NGINX Ingress Controller installation.

.PARAMETER SkipCertManager
    Skip cert-manager installation.

.PARAMETER SkipTrustManager
    Skip trust-manager installation.

.PARAMETER SkipFoundry
    Skip Foundry Local operator installation.

.PARAMETER SkipIoTOps
    Skip Azure IoT Operations installation.

.EXAMPLE
    .\scripts\02-install-platform.ps1
    .\scripts\02-install-platform.ps1 -SkipIoTOps
#>

[CmdletBinding()]
param(
    [string]$EnvFile,
    [switch]$SkipIngress,
    [switch]$SkipCertManager,
    [switch]$SkipTrustManager,
    [switch]$SkipFoundry,
    [switch]$SkipIoTOps
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── az CLI workaround ────────────────────────────────────────────────────────
# On Azure Local / restricted hosts the default extension dir may have
# permission issues.  Redirect to a writable temp folder.
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
$rgName          = if ($envVars["RESOURCE_GROUP_NAME"]) { $envVars["RESOURCE_GROUP_NAME"] } else { "$prefix-rg" }
$clusterName     = if ($envVars["CLUSTER_NAME"])        { $envVars["CLUSTER_NAME"] }        else { "$prefix-aks" }
$kvName          = if ($envVars["KEYVAULT_NAME"])        { $envVars["KEYVAULT_NAME"] }        else { "$prefix-kv" }

# Namespace / extension names
$iotOpsExtName   = if ($envVars["IOT_OPS_EXTENSION_NAME"])   { $envVars["IOT_OPS_EXTENSION_NAME"] }   else { "$prefix-iotops" }
$iotOpsNs        = if ($envVars["IOT_OPS_NAMESPACE"])        { $envVars["IOT_OPS_NAMESPACE"] }        else { "$prefix-iot" }
$mqttBrokerSvc   = if ($envVars["MQTT_BROKER_SERVICE"])      { $envVars["MQTT_BROKER_SERVICE"] }      else { "$prefix-mqtt" }
$foundryOpNs     = if ($envVars["FOUNDRY_OPERATOR_NAMESPACE"]) { $envVars["FOUNDRY_OPERATOR_NAMESPACE"] } else { "$prefix-foundry-op" }
$foundryModelNs  = if ($envVars["FOUNDRY_MODEL_NAMESPACE"])    { $envVars["FOUNDRY_MODEL_NAMESPACE"] }    else { "$prefix-foundry-mdl" }

# Ingress
$ingressCtrl     = $envVars["INGRESS_CONTROLLER"]
$ingressNs       = "$prefix-ingress"

# MetalLB
$metallbIpRange  = if ($envVars["METALLB_IP_RANGE"]) { $envVars["METALLB_IP_RANGE"] } else { "" }

# Versions
$certManagerVersion = "v1.19.2"
$trustManagerVersion = "v0.20.3"
$foundryChartUri    = "oci://mcr.microsoft.com/foundrylocalonazurelocal/helmcharts/helm/inferenceoperator"
$foundryChartVer    = "0.0.1-prp.5"
$foundryLocalChart  = Join-Path (Split-Path $PSScriptRoot) "inference-operator-$foundryChartVer.tgz"

# ── Display plan ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "┌──────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "│  Platform Install Plan                           │" -ForegroundColor DarkCyan
Write-Host "│──────────────────────────────────────────────────│" -ForegroundColor DarkCyan
Write-Host "│  Prefix:             $prefix"                       -ForegroundColor DarkCyan
Write-Host "│  Cluster:            $clusterName"                  -ForegroundColor DarkCyan
Write-Host "│  Resource Group:     $rgName"                       -ForegroundColor DarkCyan
Write-Host "│  Ingress:            $ingressCtrl → ns/$ingressNs"  -ForegroundColor DarkCyan
Write-Host "│  cert-manager:       $certManagerVersion"           -ForegroundColor DarkCyan
Write-Host "│  trust-manager:      $trustManagerVersion"          -ForegroundColor DarkCyan
Write-Host "│  Foundry Operator:   ns/$foundryOpNs"               -ForegroundColor DarkCyan
Write-Host "│  Foundry Models:     ns/$foundryModelNs"            -ForegroundColor DarkCyan
Write-Host "│  IoT Operations:     ns/$iotOpsNs"                  -ForegroundColor DarkCyan
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

# ── Step 1: NGINX Ingress Controller ────────────────────────────────────────
if (-not $SkipIngress) {
    Write-Host ""
    Write-Host "━━━ Step 1: NGINX Ingress Controller ━━━" -ForegroundColor Magenta

    # Check if already installed
    $ingressDeployment = kubectl get deployment -n $ingressNs -l app.kubernetes.io/name=ingress-nginx -o name 2>$null
    if ($ingressDeployment) {
        Write-Host "  ✅ NGINX Ingress already installed in ns/$ingressNs" -ForegroundColor Green
    } else {
        Write-Host "  📦 Adding ingress-nginx Helm repo..." -ForegroundColor Yellow
        helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>$null
        helm repo update ingress-nginx 2>$null

        Write-Host "  📦 Installing NGINX Ingress Controller..." -ForegroundColor Yellow
        helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx `
            --namespace $ingressNs `
            --create-namespace `
            --set controller.replicaCount=2 `
            --set controller.nodeSelector."kubernetes\.io/os"=linux `
            --set controller.service.type=LoadBalancer `
            --set controller.admissionWebhooks.enabled=true 2>&1
        # NOTE: --wait removed because LoadBalancer IP often stays <pending> on
        # Azure Local until MetalLB / ARC networking provisions the VIP.

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  NGINX Ingress install returned non-zero exit code. Check 'helm status ingress-nginx -n $ingressNs'."
        } else {
            Write-Host "  ✅ NGINX Ingress Controller installed" -ForegroundColor Green
        }
    }

    # ── Step 1b: MetalLB IP Pool (arcnetworking extension deploys MetalLB in kube-system)
    if ($metallbIpRange) {
        $poolExists = kubectl get ipaddresspool service-pool -n kube-system -o name 2>$null
        if ($poolExists) {
            Write-Host "  ✅ MetalLB IP pool 'service-pool' already exists" -ForegroundColor Green
        } else {
            $metallbCrdExists = kubectl get crd ipaddresspools.metallb.io -o name 2>$null
            if ($metallbCrdExists) {
                Write-Host "  📦 Applying MetalLB IP pool ($metallbIpRange)..." -ForegroundColor Yellow
@"
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: service-pool
  namespace: kube-system
spec:
  addresses:
    - $metallbIpRange
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: service-l2
  namespace: kube-system
spec:
  ipAddressPools:
    - service-pool
"@ | kubectl apply -f - 2>&1
                Write-Host "  ✅ MetalLB IP pool and L2 advertisement configured" -ForegroundColor Green
            } else {
                Write-Host "  ⚠️  MetalLB CRDs not found. Install the arcnetworking k8s-extension first." -ForegroundColor Yellow
                Write-Host "      az k8s-extension create --name arcnetworking --cluster-name $clusterName -g $rgName --cluster-type connectedClusters --extension-type microsoft.arc.networking" -ForegroundColor DarkGray
            }
        }
    } else {
        Write-Host "  ⏭️  METALLB_IP_RANGE not set — skipping MetalLB config" -ForegroundColor DarkGray
    }
} else {
    Write-Host "⏭️  Skipping NGINX Ingress (--SkipIngress)" -ForegroundColor DarkGray
}

# ── Step 2: cert-manager ────────────────────────────────────────────────────
if (-not $SkipCertManager) {
    Write-Host ""
    Write-Host "━━━ Step 2: cert-manager $certManagerVersion ━━━" -ForegroundColor Magenta

    $cmDeployment = kubectl get deployment cert-manager -n cert-manager -o name 2>$null
    if ($cmDeployment) {
        $cmVer = kubectl get deployment cert-manager -n cert-manager -o jsonpath='{.metadata.labels.app\.kubernetes\.io/version}' 2>$null
        Write-Host "  ✅ cert-manager already installed (version: $cmVer)" -ForegroundColor Green
    } else {
        Write-Host "  📦 Adding jetstack Helm repo..." -ForegroundColor Yellow
        helm repo add jetstack https://charts.jetstack.io 2>$null
        helm repo update jetstack 2>$null

        Write-Host "  📦 Installing cert-manager $certManagerVersion..." -ForegroundColor Yellow
        helm upgrade --install cert-manager jetstack/cert-manager `
            --namespace cert-manager `
            --create-namespace `
            --version $certManagerVersion `
            --set crds.enabled=true `
            --set crds.keep=true `
            --set global.leaderElection.namespace=cert-manager `
            --wait --timeout 5m 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  cert-manager install returned non-zero exit code. Check 'helm status cert-manager -n cert-manager'."
        } else {
            Write-Host "  ✅ cert-manager $certManagerVersion installed" -ForegroundColor Green
        }
    }
} else {
    Write-Host "⏭️  Skipping cert-manager (--SkipCertManager)" -ForegroundColor DarkGray
}

# ── Step 3: trust-manager ───────────────────────────────────────────────────
if (-not $SkipTrustManager) {
    Write-Host ""
    Write-Host "━━━ Step 3: trust-manager $trustManagerVersion ━━━" -ForegroundColor Magenta

    $tmDeployment = kubectl get deployment trust-manager -n cert-manager -o name 2>$null
    if ($tmDeployment) {
        $tmVer = kubectl get deployment trust-manager -n cert-manager -o jsonpath='{.metadata.labels.app\.kubernetes\.io/version}' 2>$null
        Write-Host "  ✅ trust-manager already installed (version: $tmVer)" -ForegroundColor Green
    } else {
        # cert-manager must be installed first
        $cmCheck = kubectl get deployment cert-manager -n cert-manager -o name 2>$null
        if (-not $cmCheck) {
            Write-Error "cert-manager is not installed. trust-manager depends on cert-manager. Run without -SkipCertManager first."
            exit 1
        }

        Write-Host "  📦 Installing trust-manager $trustManagerVersion..." -ForegroundColor Yellow
        helm upgrade --install trust-manager jetstack/trust-manager `
            --namespace cert-manager `
            --version $trustManagerVersion `
            --set app.trust.namespace=cert-manager `
            --wait --timeout 5m 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  trust-manager install returned non-zero exit code. Check 'helm status trust-manager -n cert-manager'."
        } else {
            Write-Host "  ✅ trust-manager $trustManagerVersion installed" -ForegroundColor Green
        }
    }
} else {
    Write-Host "⏭️  Skipping trust-manager (--SkipTrustManager)" -ForegroundColor DarkGray
}

# ── Step 4: Foundry Local Inference Operator ─────────────────────────────────
if (-not $SkipFoundry) {
    Write-Host ""
    Write-Host "━━━ Step 4: Foundry Local Inference Operator ━━━" -ForegroundColor Magenta

    # Check if the operator is already deployed
    $foundryDeploy = kubectl get deployment -n $foundryOpNs -l app.kubernetes.io/name=inferenceoperator -o name 2>$null
    if (-not $foundryDeploy) {
        # Also check by any deployment in the namespace
        $foundryDeploy = kubectl get deployment -n $foundryOpNs -o name 2>$null
    }

    if ($foundryDeploy) {
        Write-Host "  ✅ Foundry operator already installed in ns/$foundryOpNs" -ForegroundColor Green
    } else {
        # ⚠️  The Foundry Local OCI chart is Private Preview and may return 404.
        #     If the install fails, set -SkipFoundry and install manually when
        #     the chart becomes available.
        Write-Host "  ⚠️  NOTE: Foundry Local is in Private Preview — the OCI chart may not be accessible." -ForegroundColor Yellow

        # Verify cert-manager and trust-manager are running (hard deps)
        $cmReady = kubectl get deployment cert-manager -n cert-manager -o jsonpath='{.status.readyReplicas}' 2>$null
        $tmReady = kubectl get deployment trust-manager -n cert-manager -o jsonpath='{.status.readyReplicas}' 2>$null
        if (-not $cmReady -or [int]$cmReady -lt 1) {
            Write-Error "cert-manager is not ready. Foundry operator requires cert-manager."
            exit 1
        }
        if (-not $tmReady -or [int]$tmReady -lt 1) {
            Write-Error "trust-manager is not ready. Foundry operator requires trust-manager."
            exit 1
        }

        # Create operator namespace
        $nsExists = kubectl get ns $foundryOpNs -o name 2>$null
        if (-not $nsExists) {
            Write-Host "  📦 Creating namespace $foundryOpNs..." -ForegroundColor Yellow
            kubectl create namespace $foundryOpNs 2>&1
        }

        # Create model namespace
        $nsExists = kubectl get ns $foundryModelNs -o name 2>$null
        if (-not $nsExists) {
            Write-Host "  📦 Creating namespace $foundryModelNs..." -ForegroundColor Yellow
            kubectl create namespace $foundryModelNs 2>&1
        }

        # Retrieve Foundry API key from Key Vault (if stored there)
        $foundryKeyName = if ($envVars["FOUNDRY_API_KEY_NAME"]) { $envVars["FOUNDRY_API_KEY_NAME"] } else { "$prefix-foundry-key" }
        $foundryApiKey = & $azCmd keyvault secret show --vault-name $kvName --name $foundryKeyName --query value -o tsv 2>$null

        Write-Host "  📦 Installing Foundry Local operator..." -ForegroundColor Yellow
        Write-Host "      OCI Chart: $foundryChartUri" -ForegroundColor DarkGray
        Write-Host "      Local fallback: $foundryLocalChart" -ForegroundColor DarkGray
        Write-Host "      Version: $foundryChartVer" -ForegroundColor DarkGray

        $helmArgs = @(
            "upgrade", "--install", "inferenceoperator",
            $foundryChartUri,
            "--namespace", $foundryOpNs,
            "--version", $foundryChartVer,
            "--wait", "--timeout", "15m"
        )

        # Pass Foundry API key if available
        if ($foundryApiKey) {
            $helmArgs += @("--set", "apiKey=$foundryApiKey")
        }

        helm @helmArgs 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ⚠️  OCI chart failed — trying local chart fallback..." -ForegroundColor Yellow
            if (Test-Path $foundryLocalChart) {
                $helmArgs[2] = $foundryLocalChart
                $helmArgs = $helmArgs | Where-Object { $_ -ne "--version" -and $_ -ne $foundryChartVer }
                helm @helmArgs 2>&1
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "⚠️  Foundry operator install failed from local chart too."
                } else {
                    Write-Host "  ✅ Foundry Local operator installed from local chart" -ForegroundColor Green
                }
            } else {
                Write-Warning "⚠️  Local chart not found at $foundryLocalChart. Skipping Foundry operator."
            }
        } else {
            Write-Host "  ✅ Foundry Local operator installed from OCI chart" -ForegroundColor Green
        }

        # ── Create CA bundle for Foundry operator telemetry ──
        # The operator pods mount a secret/configmap named ${foundryOpNs}-ca-bundle
        # containing the root CA cert. Without this, pods stay in ContainerCreating.
        Write-Host "  📦 Creating CA bundle for Foundry operator..." -ForegroundColor Yellow
        $caSecretName = "$foundryOpNs-root-ca-key-pair"
        $caCert = kubectl get secret $caSecretName -n cert-manager -o jsonpath='{.data.ca\.crt}' 2>$null
        if (-not $caCert) {
            $caCert = kubectl get secret $caSecretName -n cert-manager -o jsonpath='{.data.tls\.crt}' 2>$null
        }
        if ($caCert) {
            $caCertDecoded = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($caCert))
            $bundleName = "$foundryOpNs-ca-bundle"
            kubectl create secret generic $bundleName -n $foundryOpNs `
                --from-literal="ca.crt=$caCertDecoded" --from-literal="ca-bundle.crt=$caCertDecoded" `
                --dry-run=client -o yaml | kubectl apply -f - 2>&1
            kubectl create configmap $bundleName -n $foundryOpNs `
                --from-literal="ca.crt=$caCertDecoded" --from-literal="ca-bundle.crt=$caCertDecoded" `
                --dry-run=client -o yaml | kubectl apply -f - 2>&1
            Write-Host "  ✅ CA bundle '$bundleName' created" -ForegroundColor Green
        } else {
            Write-Host "  ⚠️  Could not find Foundry root CA cert — pods may need manual CA bundle creation" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "⏭️  Skipping Foundry operator (--SkipFoundry)" -ForegroundColor DarkGray
}

# ── Step 5: Azure IoT Operations ────────────────────────────────────────────
if (-not $SkipIoTOps) {
    Write-Host ""
    Write-Host "━━━ Step 5: Azure IoT Operations ━━━" -ForegroundColor Magenta

    # Ensure required az CLI extensions
    Write-Host "  🔧 Ensuring az CLI extensions..." -ForegroundColor Yellow
    foreach ($ext in @("azure-iot-ops", "connectedk8s", "k8s-extension")) {
        & $azCmd extension add --name $ext --upgrade --yes 2>$null
    }
    Write-Host "  ✅ az CLI extensions ready" -ForegroundColor Green

    # Derived names for IoT Ops prerequisites
    $storageAccountName = "${prefix}iotsa"
    $drNamespace        = "${prefix}-ns"
    $schemaRegistry     = "${prefix}-sr"
    $iotOpsName         = "${prefix}-iot-ops"

    # ── Check if IoT Ops instance already exists ──
    $iotOpsState = & $azCmd iot ops show --cluster $clusterName -g $rgName --query "provisioningState" -o tsv 2>$null
    if ($iotOpsState -eq "Succeeded") {
        Write-Host "  ✅ IoT Operations instance already deployed ($iotOpsState)" -ForegroundColor Green
    } else {
        # ── 5a: Initialize IoT Operations (installs secret-store extension) ──
        Write-Host "  📦 5a: Initializing IoT Operations on cluster..." -ForegroundColor Yellow
        & $azCmd iot ops init `
            --cluster $clusterName `
            -g $rgName `
            --user-trust `
            --no-progress 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  IoT Ops init failed. Check output above."
        } else {
            Write-Host "  ✅ IoT Ops initialized (secret-store extension deployed)" -ForegroundColor Green
        }

        # ── 5b: Storage account with HNS (required for schema registry) ──
        Write-Host "  📦 5b: Creating storage account '$storageAccountName' (HNS/ADLS Gen2)..." -ForegroundColor Yellow
        $saExists = & $azCmd storage account show --name $storageAccountName -g $rgName --query "name" -o tsv 2>$null
        if ($saExists) {
            Write-Host "  ✅ Storage account '$storageAccountName' already exists" -ForegroundColor Green
        } else {
            & $azCmd storage account create `
                --name $storageAccountName -g $rgName `
                --location $location `
                --sku Standard_LRS --kind StorageV2 `
                --enable-hierarchical-namespace true 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Error "Failed to create storage account '$storageAccountName'"
                exit 1
            }
            Write-Host "  ✅ Storage account '$storageAccountName' created" -ForegroundColor Green
        }
        $saId = & $azCmd storage account show --name $storageAccountName -g $rgName --query "id" -o tsv

        # ── 5c: Device registry namespace ──
        Write-Host "  📦 5c: Creating device registry namespace '$drNamespace'..." -ForegroundColor Yellow
        $nsExists = & $azCmd iot ops ns show -n $drNamespace -g $rgName --query "name" -o tsv 2>$null
        if ($nsExists) {
            Write-Host "  ✅ Device registry namespace '$drNamespace' already exists" -ForegroundColor Green
        } else {
            & $azCmd iot ops ns create -n $drNamespace -g $rgName --location $location 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Error "Failed to create device registry namespace '$drNamespace'"
                exit 1
            }
            Write-Host "  ✅ Device registry namespace '$drNamespace' created" -ForegroundColor Green
        }
        $nsId = & $azCmd iot ops ns show -n $drNamespace -g $rgName --query "id" -o tsv

        # ── 5d: Schema registry ──
        Write-Host "  📦 5d: Creating schema registry '$schemaRegistry'..." -ForegroundColor Yellow
        $srExists = & $azCmd iot ops schema registry show --name $schemaRegistry -g $rgName --query "name" -o tsv 2>$null
        if ($srExists) {
            Write-Host "  ✅ Schema registry '$schemaRegistry' already exists" -ForegroundColor Green
        } else {
            & $azCmd iot ops schema registry create `
                --name $schemaRegistry `
                --rn $drNamespace `
                -g $rgName `
                --sa-resource-id $saId `
                --location $location 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Error "Failed to create schema registry '$schemaRegistry'"
                exit 1
            }
            Write-Host "  ✅ Schema registry '$schemaRegistry' created" -ForegroundColor Green
        }
        $srId = & $azCmd iot ops schema registry show --name $schemaRegistry -g $rgName --query "id" -o tsv

        # ── 5e: Trust infrastructure (self-signed CA chain for IoT Ops TLS) ──
        Write-Host "  📦 5e: Creating trust infrastructure..." -ForegroundColor Yellow

        # Self-signed ClusterIssuer (root)
        if (-not (kubectl get clusterissuer iot-ops-selfsigned -o name 2>$null)) {
@"
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: iot-ops-selfsigned
spec:
  selfSigned: {}
"@ | kubectl apply -f - 2>&1
        }

        # Root CA Certificate
        if (-not (kubectl get certificate iot-ops-root-ca -n cert-manager -o name 2>$null)) {
@"
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: iot-ops-root-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: iot-ops-root-ca
  secretName: iot-ops-root-ca-secret
  issuerRef:
    name: iot-ops-selfsigned
    kind: ClusterIssuer
  duration: 87600h
  renewBefore: 720h
  privateKey:
    algorithm: ECDSA
    size: 256
"@ | kubectl apply -f - 2>&1
        }

        # CA ClusterIssuer referencing the root CA secret
        if (-not (kubectl get clusterissuer iot-ops-ca-issuer -o name 2>$null)) {
@"
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: iot-ops-ca-issuer
spec:
  ca:
    secretName: iot-ops-root-ca-secret
"@ | kubectl apply -f - 2>&1
        }

        # trust-manager Bundle distributing the CA cert as a ConfigMap
        if (-not (kubectl get bundle iot-ops-trust-bundle -o name 2>$null)) {
@"
apiVersion: trust.cert-manager.io/v1alpha1
kind: Bundle
metadata:
  name: iot-ops-trust-bundle
spec:
  sources:
    - secret:
        name: iot-ops-root-ca-secret
        key: ca.crt
  target:
    configMap:
      key: trust-bundle.pem
"@ | kubectl apply -f - 2>&1
        }

        Write-Host "  ✅ Trust infrastructure ready (iot-ops-ca-issuer + trust-bundle)" -ForegroundColor Green

        # ── 5f: Create IoT Operations instance ──
        Write-Host "  📦 5f: Creating IoT Operations instance '$iotOpsName'..." -ForegroundColor Yellow
        Write-Host "      Cluster:         $clusterName" -ForegroundColor DarkGray
        Write-Host "      Schema Registry: $schemaRegistry" -ForegroundColor DarkGray
        Write-Host "      DR Namespace:    $drNamespace" -ForegroundColor DarkGray
        Write-Host "      Trust Bundle:    iot-ops-trust-bundle (trust-bundle.pem)" -ForegroundColor DarkGray

        # ── Auto-patch IoT Ops release train for regions that lack 'stable' ──
        # In some regions (e.g. southcentralus), the IoT Ops extension only supports
        # 'preview', not 'stable'. Auto-detect and patch template.py if needed.
        $templatePy = Get-ChildItem -Path $env:AZURE_EXTENSION_DIR -Recurse -Filter "template.py" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "orchestration" } | Select-Object -First 1
        if ($templatePy) {
            $tpContent = Get-Content $templatePy.FullName -Raw
            $needsPatch = $tpContent -match '"TRAINS":\s*\{"iotOperations":\s*"stable"\}'
            if ($needsPatch) {
                Write-Host "  🔧 Patching IoT Ops template.py: stable → preview train, autoUpgrade enabled..." -ForegroundColor Yellow
                # Change train to preview
                $tpContent = $tpContent -replace '"TRAINS":\s*\{"iotOperations":\s*"stable"\}', '"TRAINS":        {"iotOperations": "preview"}'
                # Remove version line for iotoperations extension (autoUpgrade=True requires no version)
                $tpContent = $tpContent -replace '(\s*"extensionType": "microsoft\.iotoperations",)\s*\n\s*"version": "[^"]+",', "`$1"
                # Enable autoUpgradeMinorVersion for iotoperations
                $tpContent = $tpContent -replace '("extensionType": "microsoft\.iotoperations",\s*"releaseTrain": "[^"]+",\s*"autoUpgradeMinorVersion":\s*)False', '$1True'
                Set-Content $templatePy.FullName -Value $tpContent -NoNewline
                Write-Host "  ✅ template.py patched (preview train, auto-upgrade)" -ForegroundColor Green
            } else {
                Write-Host "  ✅ template.py already uses preview train or custom config" -ForegroundColor Green
            }
        } else {
            Write-Host "  ⚠️  Could not find template.py — if 'stable' train fails, patch manually" -ForegroundColor Yellow
        }

        & $azCmd iot ops create `
            --cluster $clusterName `
            -g $rgName `
            --name $iotOpsName `
            --sr-resource-id $srId `
            --ns-resource-id $nsId `
            --add-insecure-listener `
            --no-progress `
            --trust-settings `
                configMapName=iot-ops-trust-bundle `
                configMapKey=trust-bundle.pem `
                issuerKind=ClusterIssuer `
                issuerName=iot-ops-ca-issuer 2>&1

        if ($LASTEXITCODE -ne 0) {
            Write-Warning "⚠️  IoT Ops create returned non-zero exit code."
            Write-Warning "    Check: & $azCmd iot ops show --cluster $clusterName -g $rgName"
        } else {
            Write-Host "  ✅ IoT Operations instance '$iotOpsName' created successfully" -ForegroundColor Green
        }
    }
} else {
    Write-Host "⏭️  Skipping IoT Operations (--SkipIoTOps)" -ForegroundColor DarkGray
}

# ── Step 6: Verify all platform components ──────────────────────────────────
Write-Host ""
Write-Host "━━━ Verification ━━━" -ForegroundColor Magenta
Write-Host ""

$allGood = $true

# Check NGINX Ingress
if (-not $SkipIngress) {
    $ingressReady = kubectl get deployment -n $ingressNs -l app.kubernetes.io/name=ingress-nginx -o jsonpath='{.items[0].status.readyReplicas}' 2>$null
    if ($ingressReady -and [int]$ingressReady -ge 1) {
        Write-Host "  ✅ NGINX Ingress:       ready ($ingressReady replicas)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ NGINX Ingress:       not ready" -ForegroundColor Red
        $allGood = $false
    }
}

# Check cert-manager
if (-not $SkipCertManager) {
    $cmReady = kubectl get deployment cert-manager -n cert-manager -o jsonpath='{.status.readyReplicas}' 2>$null
    if ($cmReady -and [int]$cmReady -ge 1) {
        Write-Host "  ✅ cert-manager:        ready ($cmReady replicas)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ cert-manager:        not ready" -ForegroundColor Red
        $allGood = $false
    }
}

# Check trust-manager
if (-not $SkipTrustManager) {
    $tmReady = kubectl get deployment trust-manager -n cert-manager -o jsonpath='{.status.readyReplicas}' 2>$null
    if ($tmReady -and [int]$tmReady -ge 1) {
        Write-Host "  ✅ trust-manager:       ready ($tmReady replicas)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ trust-manager:       not ready" -ForegroundColor Red
        $allGood = $false
    }
}

# Check Foundry operator
if (-not $SkipFoundry) {
    $foReady = kubectl get deployment -n $foundryOpNs -o jsonpath='{.items[0].status.readyReplicas}' 2>$null
    if ($foReady -and [int]$foReady -ge 1) {
        Write-Host "  ✅ Foundry operator:    ready ($foReady replicas)" -ForegroundColor Green
    } else {
        Write-Host "  ❌ Foundry operator:    not ready" -ForegroundColor Red
        $allGood = $false
    }
}

# Check IoT Operations instance
if (-not $SkipIoTOps) {
    $iotState = & $azCmd iot ops show --cluster $clusterName -g $rgName --query "provisioningState" -o tsv 2>$null
    if ($iotState -eq "Succeeded") {
        Write-Host "  ✅ IoT Operations:      $iotState" -ForegroundColor Green
        # Also show MQTT broker service
        $mqttSvc = kubectl get svc aio-broker-insecure -n azure-iot-operations -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
        if ($mqttSvc) {
            Write-Host "     MQTT broker (insecure): ${mqttSvc}:1883" -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  ❌ IoT Operations:      $iotState" -ForegroundColor Red
        $allGood = $false
    }
}

Write-Host ""
if ($allGood) {
    Write-Host "🎉 All platform components are healthy!" -ForegroundColor Green
} else {
    Write-Host "⚠️  Some components are not ready. Review warnings above." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Platform install complete. Next steps:" -ForegroundColor Cyan
Write-Host "  1. Deploy IoT Hub & drone sim: .\scripts\03-deploy-iot-simulation.ps1" -ForegroundColor Cyan
Write-Host "  2. Deploy a Foundry model:     kubectl apply -f manifests/foundry-model.yaml" -ForegroundColor Cyan
Write-Host "  3. Deploy demo workloads:      .\scripts\04-deploy-workloads.ps1" -ForegroundColor Cyan
Write-Host ""
