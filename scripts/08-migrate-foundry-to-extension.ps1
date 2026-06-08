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
    LESSONS LEARNED FROM vi-portland CUTOVER (2026-06-08, COMPLETED SUCCESSFULLY):

    1. NEW CRD SCHEMA — the Microsoft.Foundry extension ships DIFFERENT CRDs vs
       the old `inferenceoperator@0.0.1-prp.5` Helm chart. The OLD
       k8s/foundry-local.yaml + k8s/vi-foundry-local.yaml will FAIL with
       strict-decoding errors. Use k8s/foundry-local-extension.yaml instead.

       The shipping pattern for CATALOG (built-in) models is just a
       ModelDeployment with `model.catalog.name: phi-4-mini` — the operator
       auto-creates the StoreModel + Model behind the scenes. You do NOT need to
       hand-author a StoreModel CR for catalog models.

       Key schema differences:
         * `model.ref` (string) targets a hand-authored Model CR (BYO)
         * `model.catalog.{name,version}` targets the foundry-local-catalog ConfigMap
         * `model.custom` targets an OCI registry (BYO with credentials)
         * `model.maas` proxies to a hosted MaaS endpoint
         * `spec.compute` REQUIRED (cpu|gpu); MUST match StoreModel.compute
         * `spec.workloadType` REQUIRED (generative|predictive)
         * `spec.runtime` optional (onnx-genai|vllm|maas)
         * `spec.resources.limits.gpu` — bare `gpu`, NOT `nvidia.com/gpu`
         * `spec.authentication` REMOVED — auth handled by entra-sidecar

    2. CRs MUST LIVE IN THE OPERATOR'S NAMESPACE (`foundry-local-operator`). The
       cache Job that downloads the model references ConfigMap
       `foundry-otel-sidecar-config` by name without namespace — only resolves
       if the CR is in the same namespace as the operator's release-namespace.
       Putting CRs in a separate namespace causes silent FailedMount loops.

    3. RESOURCE FOOTPRINT & FAILED EXTENSION STATE — the extension's `model-store`
       SUBCHART deployment HARDCODES registry container cpu:1 + nginx-sidecar
       cpu:2 = 3 CPU. Helm values `modelStore.registry.resources.requests.cpu`
       and `modelStore.nginxSidecar.resources.requests.cpu` are DEFINED in
       values.yaml but the subchart template DOES NOT reference them — passing
       via `az k8s-extension --config` is silently ignored. (Upstream chart bug.)

       In contrast, `api.config.server.workers` IS a real, wired key
       (api-deployment.yaml line ~170). This script now passes
       `--config api.config.server.workers=1` at create time, eliminating the
       WORKERS=4 → uvicorn-recycle → CrashLoopBackOff cycle at the chart level.

       The model-store hardcoding still requires the patch-guardian CronJob to
       hold cpu:200m/100m so it schedules on undersized GPU nodes (e.g.,
       Standard_NC4_A2 with 4 CPU has ~2.4 CPU free — can't fit 3 CPU request).
       The default RollingUpdate strategy on model-store also hits PVC
       Multi-Attach (RWO PVC held by old pod blocks new pod) on every helm
       reconcile, so this script now patches strategy=Recreate to avoid that.

       Even with all of the above, the extension agent reconciles every ~5-10
       min and the helm-upgrade WILL appear Failed if the guardian's patched
       model-store deployment hasn't been observed Ready within the helm
       timeout window. Once the guardian runs after a reconcile, the extension
       returns to Succeeded. Future remediation: scale GPU node to NC8+ (8 CPU)
       to fit upstream chart defaults, OR upstream fix to wire modelStore
       resource values. Track upstream issue before removing the guardian.

    4. INFERENCE POD SCHEDULING — the inference pod has 6 containers
       (model-store-pull init + otel-sidecar + msi-adapter + inference +
       nginx-sidecar + entra-sidecar) totalling ~2.75Gi memory / 1 CPU
       effective request, AND requires `gpu: 1`. On a single-GPU node cluster,
       the pod MUST schedule on that GPU node. Plan for >=3Gi free mem on the
       GPU node BEFORE applying the ModelDeployment. On vi-portland we had to
       evict inference-operator-api off the GPU node + scale down
       video-indexer-completion + video-dashboard to make room.

    5. AKS-Admins MEMBERSHIP — both pdx clusters have enableAzureRbac:false and
       gate kubectl access on AAD group `AKS-Admins` (094db372-f9b2-4477-937c-
       869b8cf2bb8a). The operator must be in this group OR have Arc-side admin
       added via `az role assignment create --role 'Azure Arc Kubernetes Cluster
       Admin'`. RBAC role alone is not sufficient on legacy-AAD clusters.

    6. HELM RELEASE NAME — the old chart release is `inferenceoperator` (one
       word), NOT `inference-operator`. The helm-cleanup stage already accounts
       for this; do not "correct" it.

    7. API ENDPOINT REQUIRES ENTRA AUTH — the model server inside the pod is
       reachable at http://localhost:5000 inside the inference container, but
       all external traffic must go through nginx-sidecar:8443 (TLS) →
       entra-sidecar (validates Bearer tokens). For smoke tests use
       `kubectl exec ... -c inference -- curl http://localhost:5000/v1/models`
       which lists the loaded model. Chat completions also require Bearer
       tokens even on localhost.

    8. CO-RESIDENT videoindexer EXTENSION (vi-portland pattern) — when Foundry
       runs on the same GPU node as the Microsoft.VideoIndexer extension, two
       chart defaults will cause videoindexer Failed state and must be overridden
       via `az k8s-extension update`:

         storage.indexing.size=50Gi
           Chart default is 500Gi; PVC is provisioned at 50Gi on existing
           clusters. Longhorn refuses to expand if disk free < requested delta.
           Pinning to current size prevents helm-upgrade from attempting
           expansion. Template: vi-pvc.yaml -> _storage.tpl helper.

         scaling.webapi.minReplicaCount=1
           Chart default is 3. Each webapi pod requests 500m CPU + 4Gi RAM.
           With Foundry co-resident, the cluster only fits 1 webapi replica.
           HPA min=1 stops creating perpetually-Pending pods. Template:
           webapp-hpa.yaml.

       Apply via:
         az k8s-extension update --name videoindexer -c <cluster> -g <rg> \
           --cluster-type connectedClusters --yes \
           --config storage.indexing.size=50Gi \
           --config scaling.webapi.minReplicaCount=1

       Discovery pattern (reusable for any chart bug): pull the helm release
       secret, gzip-decode the .data.release blob, inspect chart.templates +
       chart.values to find which values are actually wired vs hardcoded.
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
                    --config entraAuth.clientId=$FoundryClientId `
                    --config api.config.server.workers=1
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

        $repoRoot = Split-Path $PSScriptRoot -Parent
        $manifest = Join-Path $repoRoot 'k8s/foundry-local-extension.yaml'
        $guardian = Join-Path $repoRoot 'k8s/foundry-patch-guardian.yaml'
        if (-not (Test-Path $manifest)) { throw "Manifest not found: $manifest" }
        if (-not (Test-Path $guardian)) { throw "Guardian not found: $guardian" }
        Write-Host "    Source manifest: $manifest" -ForegroundColor DarkGray
        Write-Host "    Guardian:        $guardian" -ForegroundColor DarkGray

        Invoke-Step "kubectl apply -f $manifest" {
            kubectl apply -f $manifest
        }

        # Patch model-store Deployment strategy to Recreate (default RollingUpdate hits
        # PVC Multi-Attach: old RWO pod blocks new pod on every helm reconcile).
        # Wait for the Deployment to exist (extension agent creates it asynchronously),
        # then patch. Idempotent.
        Invoke-Step "patch model-store strategy=Recreate (avoid PVC Multi-Attach on reconciles)" {
            $deadline = (Get-Date).AddMinutes(5)
            while ((Get-Date) -lt $deadline) {
                $exists = kubectl get deploy -n $newOpNs inference-operator-model-store --ignore-not-found -o name 2>$null
                if ($exists) { break }
                Start-Sleep -Seconds 10
            }
            if (-not $exists) {
                Write-Warning '  model-store Deployment not present after 5min; skipping strategy patch'
            } else {
                kubectl patch deploy -n $newOpNs inference-operator-model-store --type=strategic `
                    -p '{"spec":{"strategy":{"$retainKeys":["type"],"type":"Recreate"}}}'
            }
        }

        # Guardian re-patches model-store cpu/mem (subchart hardcodes registry cpu:1 +
        # nginx-sidecar cpu:2 — undersized GPU nodes can't schedule). WORKERS=1 is now
        # set via extension --config so guardian's WORKERS branch is a defense-in-depth
        # no-op. See header note #3 for details.
        Invoke-Step "kubectl apply -f $guardian (patch-revert workaround)" {
            kubectl apply -f $guardian
        }
        Invoke-Step "trigger initial guardian run" {
            kubectl create job --from=cronjob/foundry-patch-guardian "foundry-patch-guardian-init-$(Get-Date -Format yyyyMMddHHmmss)" -n foundry-local-operator | Out-Null
        }
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
