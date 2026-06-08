<#
.SYNOPSIS
    Migrate Foundry Local from Helm chart -> Microsoft.Foundry Azure Arc extension.

.DESCRIPTION
    Idempotent, staged cutover for one AKS Arc cluster at a time. Same script
    runs locally, in Azure Cloud Shell, in a Codespace, or via GitHub Actions.

    Stages (run individually or pass -Stage full):
      1. backup         Dump existing Model/ModelDeployment CRs to ./foundry-backup-<cluster>.yaml
      2. helm-cleanup   helm uninstall inference-operator, trust-manager, cert-manager (CRDs preserved)
      3. cert-manager   az k8s-extension create Microsoft.CertManagement
      4. foundry        az k8s-extension create Microsoft.Foundry (requires -FoundryClientId)
      5. apply-models   Re-apply Model/ModelDeployment in foundry-local-operator namespace
      6. verify         kubectl checks + extension provisioningState

    Stage `full` runs 1->6. Stage `prep` runs 1->3 (safe without app reg).

.PARAMETER ClusterEnvFile
    Path to config/<cluster>.env (e.g. config/vi-portland.env or config/pdx-mwc-26.env).

.PARAMETER Stage
    backup | helm-cleanup | cert-manager | foundry | apply-models | verify | prep | full

.PARAMETER FoundryClientId
    Entra app registration client ID for Foundry operator. Required for `foundry` and `full` stages.
    Falls back to env var FOUNDRY_APP_CLIENT_ID if not supplied.

.PARAMETER TenantId
    Entra tenant ID. Defaults to 72f988bf-86f1-41af-91ab-2d7cd011db47 (Microsoft corp).

.PARAMETER SkipProxy
    Skip starting `az connectedk8s proxy`. Use when kubeconfig is already on PATH
    (e.g. inside a GitHub Actions step that started the proxy in a prior step).

.PARAMETER DryRun
    Print every state-changing command without executing it.

.EXAMPLE
    # Pilot: vi-portland, full cutover
    ./scripts/08-migrate-foundry-to-extension.ps1 `
        -ClusterEnvFile ./config/vi-portland.env `
        -Stage full `
        -FoundryClientId 11111111-2222-3333-4444-555555555555

.EXAMPLE
    # Prep only (safe — no app reg needed yet)
    ./scripts/08-migrate-foundry-to-extension.ps1 `
        -ClusterEnvFile ./config/pdx-mwc-26.env -Stage prep

.NOTES
    LESSONS LEARNED FROM vi-portland CUTOVER (2026-06-08):

    1. NEW CRD SCHEMA -- the Microsoft.Foundry extension SHIPS DIFFERENT CRDs vs
       the old `inferenceoperator@0.0.1-prp.5` Helm chart. The `apply-models`
       stage in this script applies the OLD CRs from k8s/foundry-local.yaml and
       will FAIL with strict-decoding errors. The new shape is:

         * StoreModel (NEW kind) -- declares the model SOURCE
             spec: { source: foundry-local, alias: phi-4-mini, compute: gpu,
                     framework: onnx }
         * Model (RESHAPED) -- pure metadata; the operator creates it AFTER the
           StoreModel cache pod finishes downloading
         * ModelDeployment (RESHAPED) -- spec.authentication is REMOVED;
           spec.resources.limits.gpu (NOT nvidia.com/gpu) for GPU; required:
           model, workloadType, compute; new optional runtime: onnx-genai|vllm|maas

       See k8s/foundry-local-extension.yaml for a working new-schema template.
       The apply-models stage in THIS script still uses the old manifests and
       MUST be updated before re-running on pdx-mwc-26.

    2. RESOURCE FOOTPRINT -- the new extension's model-store deployment requests
       1 CPU (registry) + 2 CPU (nginx-sidecar) = 3 CPU. On capacity-constrained
       clusters (vi-portland: 4 nodes ~4 CPU each, ~95% committed pre-migration)
       Helm install times out because no node has 3 free CPU. Workarounds tried:
         * `--config modelStore.resources.requests.cpu=...` -- IGNORED (wrong key)
         * kubectl patch deployment after creation -- WORKS briefly, but the
           extension agent re-runs `helm upgrade` and REVERTS the patch.
       The correct fix requires finding the chart's actual values keys; until
       then plan for >=3 free CPU and >=1Gi free mem on at least one worker.

    3. WORKERS=4 BUG in inference-operator-api image -- with WORKERS=4 the
       uvicorn workers recycle every ~1s, liveness probe (curl /healthz) catches
       the gap, SIGTERMs the container, CrashLoopBackOff forever. WORKERS=1
       fixes it. Same caveat as #2: kubectl patch is reverted by the agent.

    4. AKS-Admins MEMBERSHIP -- both pdx clusters have enableAzureRbac:false and
       gate kubectl access on AAD group `AKS-Admins` (094db372-f9b2-4477-937c-
       869b8cf2bb8a). The operator must be in this group OR have Arc-side admin
       added via `az role assignment create --role 'Azure Arc Kubernetes Cluster
       Admin'`. RBAC role alone is not sufficient on legacy-AAD clusters.

    5. HELM RELEASE NAME -- the old chart release is `inferenceoperator` (one
       word), NOT `inference-operator`. The helm-cleanup stage already accounts
       for this; do not "correct" it.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string]$ClusterEnvFile,

    [Parameter(Mandatory=$true)]
    [ValidateSet('backup','helm-cleanup','cert-manager','foundry','apply-models','verify','prep','full')]
    [string]$Stage,

    [string]$FoundryClientId = $env:FOUNDRY_APP_CLIENT_ID,
    [string]$TenantId,
    [switch]$SkipProxy,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ─── Helpers ────────────────────────────────────────────────────────────────
$azCmd = if ($IsWindows -or $env:OS -match 'Windows') {
    (Get-Command az.cmd -ErrorAction SilentlyContinue)?.Source ?? 'az'
} else { 'az' }

function Invoke-Step {
    param([string]$Description, [scriptblock]$Action)
    Write-Host "  → $Description" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "    [DRY-RUN] $($Action.ToString().Trim())" -ForegroundColor DarkYellow
    } else {
        & $Action
    }
}

function Read-Env {
    param([string]$Path)
    $h = @{}
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith('#')) {
            $i = $line.IndexOf('=')
            if ($i -gt 0) {
                $h[$line.Substring(0,$i).Trim()] =
                    $line.Substring($i+1).Trim().Trim('"').Trim("'")
            }
        }
    }
    return $h
}

# ─── Load env ───────────────────────────────────────────────────────────────
if (-not (Test-Path $ClusterEnvFile)) {
    throw "Env file not found: $ClusterEnvFile"
}
$env_ = Read-Env $ClusterEnvFile
$prefix      = $env_['PREFIX']
$cluster     = $env_['CLUSTER_NAME']
$rg          = $env_['RESOURCE_GROUP_NAME']
$subId       = $env_['SUBSCRIPTION_ID']
$oldOpNs     = if ($env_['FOUNDRY_OPERATOR_NAMESPACE']) { $env_['FOUNDRY_OPERATOR_NAMESPACE'] } else { "$prefix-foundry-op" }
$oldModelNs  = if ($env_['FOUNDRY_MODEL_NAMESPACE'])    { $env_['FOUNDRY_MODEL_NAMESPACE'] }    else { "$prefix-foundry-mdl" }
$newOpNs     = 'foundry-local-operator'

Write-Host ''
Write-Host '┌──────────────────────────────────────────────────────────────┐' -ForegroundColor DarkMagenta
Write-Host '│  Foundry Local: Helm → Arc Extension Migration               │' -ForegroundColor DarkMagenta
Write-Host '├──────────────────────────────────────────────────────────────┤' -ForegroundColor DarkMagenta
Write-Host "│  Cluster:           $cluster"      -ForegroundColor DarkMagenta
Write-Host "│  Resource Group:    $rg"           -ForegroundColor DarkMagenta
Write-Host "│  Subscription:      $subId"        -ForegroundColor DarkMagenta
Write-Host "│  Old operator ns:   $oldOpNs"      -ForegroundColor DarkMagenta
Write-Host "│  Old models ns:     $oldModelNs"   -ForegroundColor DarkMagenta
Write-Host "│  New operator ns:   $newOpNs (fixed by extension)" -ForegroundColor DarkMagenta
Write-Host "│  Stage:             $Stage"        -ForegroundColor DarkMagenta
Write-Host "│  Dry-run:           $DryRun"       -ForegroundColor DarkMagenta
Write-Host '└──────────────────────────────────────────────────────────────┘' -ForegroundColor DarkMagenta
Write-Host ''

# ─── Prereqs ────────────────────────────────────────────────────────────────
foreach ($t in @('kubectl','helm')) {
    if (-not (Get-Command $t -ErrorAction SilentlyContinue)) {
        throw "Required tool '$t' not on PATH."
    }
}
& $azCmd account set --subscription $subId | Out-Null

# Default tenant to the currently-active az tenant if not provided
if (-not $TenantId) {
    $TenantId = & $azCmd account show --query tenantId -o tsv
    Write-Host "Using tenant from az context: $TenantId" -ForegroundColor DarkGray
}

# ─── Optional: connectedk8s proxy ──────────────────────────────────────────
$proxyJob = $null
if (-not $SkipProxy) {
    Write-Host 'Starting az connectedk8s proxy (background)...' -ForegroundColor DarkGray
    if (-not $DryRun) {
        $kubeconfigPath = Join-Path $env:TEMP "kubeconfig-$cluster"
        $proxyJob = Start-Job -ScriptBlock {
            param($az,$c,$r,$kc) & $az connectedk8s proxy --name $c -g $r --file $kc
        } -ArgumentList $azCmd,$cluster,$rg,$kubeconfigPath

        # Wait up to 60s for kubeconfig to materialize and a simple query to succeed
        $env:KUBECONFIG = $kubeconfigPath
        $ok = $false
        for ($i=0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 2
            try { kubectl get ns -o name 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $ok = $true; break } } catch { }
        }
        if (-not $ok) { throw "kubectl could not reach $cluster via connectedk8s proxy after 60s." }
        Write-Host '  ✅ kubectl reachable via Arc proxy' -ForegroundColor Green
    }
}

try {
    # ─── Stage: backup ──────────────────────────────────────────────────────
    if ($Stage -in @('backup','prep','full')) {
        Write-Host ''; Write-Host '━━━ Stage: backup ━━━' -ForegroundColor Magenta
        $backupFile = "foundry-backup-$cluster-$(Get-Date -f yyyyMMdd-HHmmss).yaml"
        Invoke-Step "Dump existing Model and ModelDeployment CRs to $backupFile" {
            kubectl get models.foundrylocal.azure.com,modeldeployments.foundrylocal.azure.com `
                -A -o yaml 2>$null | Out-File -FilePath $backupFile -Encoding UTF8
        }
        if (-not $DryRun -and (Test-Path $backupFile)) {
            $bytes = (Get-Item $backupFile).Length
            Write-Host "    Saved $bytes bytes" -ForegroundColor DarkGray
        }
    }

    # ─── Stage: helm-cleanup ────────────────────────────────────────────────
    if ($Stage -in @('helm-cleanup','prep','full')) {
        Write-Host ''; Write-Host '━━━ Stage: helm-cleanup ━━━' -ForegroundColor Magenta

        Invoke-Step "Delete CRs in $oldModelNs" {
            kubectl delete modeldeployment --all -n $oldModelNs --ignore-not-found 2>$null
            kubectl delete model            --all -n $oldModelNs --ignore-not-found 2>$null
        }

        Invoke-Step 'helm uninstall inferenceoperator (keep CRDs)' {
            helm uninstall inferenceoperator -n $oldOpNs 2>$null
            # CRDs are cluster-scoped; chart should not own them. Verify they remain.
            $crd = kubectl get crd models.foundrylocal.azure.com -o name 2>$null
            if (-not $crd) {
                Write-Warning '    Foundry CRDs were removed by helm uninstall. The Arc extension will recreate them on install.'
            }
        }

        Invoke-Step 'helm uninstall trust-manager' {
            helm uninstall trust-manager -n cert-manager 2>$null
        }
        Invoke-Step 'helm uninstall cert-manager' {
            helm uninstall cert-manager -n cert-manager 2>$null
        }
    }

    # ─── Stage: cert-manager (Arc extension) ────────────────────────────────
    if ($Stage -in @('cert-manager','prep','full')) {
        Write-Host ''; Write-Host '━━━ Stage: cert-manager (Microsoft.CertManagement) ━━━' -ForegroundColor Magenta
        Invoke-Step 'az k8s-extension create Microsoft.CertManagement' {
            & $azCmd k8s-extension create `
                --cluster-name $cluster `
                --resource-group $rg `
                --cluster-type connectedClusters `
                --name azure-cert-manager `
                --extension-type Microsoft.CertManagement `
                --scope cluster `
                --release-train stable `
                --config config.enableGatewayAPI=true `
                --config cert-manager.crds.keep=true `
                --config trust-manager.defaultPackage.enabled=false `
                --config trust-manager.secretTargets.enabled=true `
                --config trust-manager.secretTargets.authorizedSecretsAll=true
        }
        if (-not $DryRun) {
            $state = & $azCmd k8s-extension show -g $rg --cluster-name $cluster `
                --cluster-type connectedClusters --name azure-cert-manager `
                --query 'provisioningState' -o tsv
            Write-Host "    provisioningState = $state" -ForegroundColor $(if ($state -eq 'Succeeded') { 'Green' } else { 'Yellow' })
        }
    }

    # ─── Stage: foundry (Arc extension) ─────────────────────────────────────
    if ($Stage -in @('foundry','full')) {
        Write-Host ''; Write-Host '━━━ Stage: foundry (Microsoft.Foundry) ━━━' -ForegroundColor Magenta
        if (-not $FoundryClientId) {
            Write-Warning 'FoundryClientId not supplied and FOUNDRY_APP_CLIENT_ID env var is empty.'
            Write-Warning 'Cannot install Microsoft.Foundry without an Entra app registration client ID.'
            Write-Warning 'Skipping `foundry` stage. Re-run with -FoundryClientId <guid> once app reg exists.'
        } else {
            Invoke-Step 'az k8s-extension create Microsoft.Foundry' {
                & $azCmd k8s-extension create `
                    --resource-group $rg `
                    --cluster-name $cluster `
                    --cluster-type connectedClusters `
                    --name inference-operator `
                    --extension-type Microsoft.Foundry `
                    --scope cluster `
                    --release-namespace $newOpNs `
                    --auto-upgrade-minor-version true `
                    --release-train stable `
                    --config entraAuth.tenantId=$TenantId `
                    --config entraAuth.clientId=$FoundryClientId
            }
            if (-not $DryRun) {
                $state = & $azCmd k8s-extension show -g $rg --cluster-name $cluster `
                    --cluster-type connectedClusters --name inference-operator `
                    --query 'provisioningState' -o tsv
                Write-Host "    provisioningState = $state" -ForegroundColor $(if ($state -eq 'Succeeded') { 'Green' } else { 'Yellow' })
            }
        }
    }

    # ─── Stage: apply-models ────────────────────────────────────────────────
    if ($Stage -in @('apply-models','full')) {
        Write-Host ''; Write-Host '━━━ Stage: apply-models ━━━' -ForegroundColor Magenta

        # Pick the right manifest based on cluster prefix
        $repoRoot = Split-Path $PSScriptRoot -Parent
        $manifest = switch -Regex ($cluster) {
            '^vi-'   { Join-Path $repoRoot 'k8s/vi-foundry-local.yaml' }
            default  { Join-Path $repoRoot 'k8s/foundry-local.yaml' }
        }
        if (-not (Test-Path $manifest)) { throw "Manifest not found: $manifest" }
        Write-Host "    Source manifest: $manifest" -ForegroundColor DarkGray

        # Rewrite the namespace line-by-line (anchored end-of-line) to foundry-local-operator.
        # Does NOT mutate the repo file — writes a temp copy.
        $rendered = Get-Content $manifest | ForEach-Object {
            $_ -replace '^(\s*namespace:\s*)foundry-local\s*$',    "`${1}$newOpNs" `
               -replace '^(\s*namespace:\s*)vi-foundry-local\s*$', "`${1}$newOpNs"
        }

        $tmpYaml = Join-Path ([System.IO.Path]::GetTempPath()) "foundry-$([Guid]::NewGuid()).yaml"
        $rendered | Set-Content -Path $tmpYaml -Encoding UTF8

        Invoke-Step "kubectl apply -f (rendered, namespace=$newOpNs)" {
            kubectl apply -f $tmpYaml
        }
        if (-not $DryRun) { Remove-Item $tmpYaml -ErrorAction SilentlyContinue }
    }

    # ─── Stage: verify ──────────────────────────────────────────────────────
    if ($Stage -in @('verify','full')) {
        Write-Host ''; Write-Host '━━━ Stage: verify ━━━' -ForegroundColor Magenta
        Invoke-Step 'Extension status' {
            & $azCmd k8s-extension list -g $rg --cluster-name $cluster `
                --cluster-type connectedClusters `
                --query "[?name=='azure-cert-manager' || name=='inference-operator'].{name:name,type:extensionType,state:provisioningState,version:version}" `
                -o table
        }
        Invoke-Step "Operator pods in $newOpNs" {
            kubectl get pods -n $newOpNs
        }
        Invoke-Step 'Model deployments' {
            kubectl get modeldeployment -A
        }
    }

    Write-Host ''
    Write-Host '✅ Migration stage(s) completed.' -ForegroundColor Green
    if ($Stage -in @('foundry','full') -and $FoundryClientId) {
        Write-Host ''
        Write-Host '⚠️  REMINDER: Consumer apps (drone-demo, video-dashboard) still use api-key auth.' -ForegroundColor Yellow
        Write-Host '   They will return 401 from Foundry until rewritten to mint Entra bearer tokens.' -ForegroundColor Yellow
        Write-Host '   See docs/foundry-arc-extension-migration.md Step 6 + Risk table.' -ForegroundColor Yellow
    }
}
finally {
    if ($proxyJob) {
        Stop-Job $proxyJob -ErrorAction SilentlyContinue
        Remove-Job $proxyJob -Force -ErrorAction SilentlyContinue
    }
}
