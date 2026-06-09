<#
.SYNOPSIS
    Provision Entra ID security groups and Azure RBAC role assignments for
    Foundry Local (Microsoft.Foundry Arc extension) operators.

.DESCRIPTION
    Creates three Entra groups (idempotent) and assigns Azure built-in roles
    at SUBSCRIPTION scope so members can self-serve Foundry Local lifecycle
    on any Arc-connected AKS Arc cluster in the subscription.

    Groups created:
      ACX-FoundryLocal-Readers         (view-only — extensions, models, status)
      ACX-FoundryLocal-Contributors    (deploy extensions, use kubectl)
      ACX-FoundryLocal-Administrators  (full lifecycle, including delete)

    Why subscription-scope (not RG-scope):
      Foundry Local touches multiple RGs per cluster (HCI cluster RG, AKS Arc
      cluster RG, custom-location RG, image-store RG). Sub-scope avoids the
      cross-RG visibility holes that bite operators in the field.

    Role design rationale:

      Readers ─── Reader (sub-scope)
        Lets members read Microsoft.KubernetesConfiguration/extensions,
        connectedClusters, customLocations, plus everything else they'd need
        to triage. Does NOT grant `listClusterUserCredential` so they cannot
        get kubeconfigs — they're truly read-only on ARM.

      Contributors ─── Kubernetes Extension Contributor
                   +   Azure Arc Enabled Kubernetes Cluster User Role
        Extension Contributor grants Microsoft.KubernetesConfiguration/* on
        extensions + sourceControlConfigurations + fluxConfigurations —
        exactly what `az k8s-extension create/update/delete` needs.
        Cluster User Role adds listClusterUserCredential so they can run
        `az connectedk8s proxy` and then kubectl. They CANNOT delete the
        cluster, change identity, or alter custom locations.

      Administrators ─── Contributor (sub-scope)
        Full lifecycle: create/delete connected clusters, custom locations,
        Arc agents, plus everything the other two tiers can do. Use sparingly.

    Cluster-side RBAC (separate concern):
      On clusters with enableAzureRbac=false (e.g., pdx-mwc-26 today) the
      cluster gates kubectl access on the AAD group AKS-Admins
      (094db372-f9b2-4477-937c-869b8cf2bb8a). Add Foundry admins/contributors
      to AKS-Admins SEPARATELY — this script only handles ARM-side RBAC.

      On clusters with enableAzureRbac=true (recommended for new deploys),
      grant `Azure Arc Kubernetes Cluster Admin` at cluster scope instead of
      relying on the AKS-Admins group.

.PARAMETER SubscriptionId
    Target subscription. Defaults to AdaptiveCloudLab
    (fbaf508b-cb61-4383-9cda-a42bfa0c7bc9).

.PARAMETER TenantId
    Entra tenant. Defaults to d1623670-9777-4399-aaf6-01d87b84ef1d.

.PARAMETER MailNickname
    Set if your tenant requires a non-default mailNickname for groups.
    Defaults to the group's displayName lowercased without hyphens.

.PARAMETER DryRun
    Print planned commands without executing.

.EXAMPLE
    ./scripts/09-foundrylocal-rbac.ps1                        # provision defaults
    ./scripts/09-foundrylocal-rbac.ps1 -DryRun                # preview
    ./scripts/09-foundrylocal-rbac.ps1 -SubscriptionId <id>   # different sub
#>

[CmdletBinding()]
param(
    [string]$SubscriptionId = 'fbaf508b-cb61-4383-9cda-a42bfa0c7bc9',
    [string]$TenantId       = 'd1623670-9777-4399-aaf6-01d87b84ef1d',
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$azCmd = if ($IsWindows -or $env:OS -match 'Windows') {
    (Get-Command az.cmd -ErrorAction SilentlyContinue)?.Source ?? 'az'
} else { 'az' }

function Invoke-Step {
    param([string]$Description, [scriptblock]$Action)
    Write-Host "  -> $Description" -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host "    [DRY-RUN] $($Action.ToString().Trim())" -ForegroundColor DarkYellow
        return $null
    }
    return & $Action
}

# Group → role spec. Each role is assigned at /subscriptions/$SubscriptionId.
$plan = @(
    @{
        Name        = 'ACX-FoundryLocal-Readers'
        Description = 'View-only access to Foundry Local extensions and Arc resources. No kubeconfig access.'
        Roles       = @('Reader')
    },
    @{
        Name        = 'ACX-FoundryLocal-Contributors'
        Description = 'Deploy/update Foundry Local extensions and use kubectl via Arc proxy. Cannot delete clusters or custom locations.'
        Roles       = @(
            'Kubernetes Extension Contributor'
            'Azure Arc Enabled Kubernetes Cluster User Role'
        )
    },
    @{
        Name        = 'ACX-FoundryLocal-Administrators'
        Description = 'Full Foundry Local lifecycle including cluster/custom-location admin. Use sparingly.'
        Roles       = @('Contributor')
    }
)

# ─── Banner ─────────────────────────────────────────────────────────────────
Write-Host ''
Write-Host '┌──────────────────────────────────────────────────────────────┐' -ForegroundColor Cyan
Write-Host '│  Foundry Local: Entra Groups + Subscription RBAC             │' -ForegroundColor Cyan
Write-Host '├──────────────────────────────────────────────────────────────┤' -ForegroundColor Cyan
Write-Host "│  Subscription:  $SubscriptionId" -ForegroundColor Cyan
Write-Host "│  Tenant:        $TenantId" -ForegroundColor Cyan
Write-Host "│  Dry-run:       $DryRun" -ForegroundColor Cyan
Write-Host '└──────────────────────────────────────────────────────────────┘' -ForegroundColor Cyan
Write-Host ''

# ─── Verify az context ──────────────────────────────────────────────────────
$ctx = & $azCmd account show -o json 2>$null | ConvertFrom-Json
if (-not $ctx) { throw 'Not signed in. Run: az login' }
if ($ctx.tenantId -ne $TenantId) {
    Write-Warning "Current tenant ($($ctx.tenantId)) != target tenant ($TenantId). Re-login if this is wrong."
}
if ($ctx.id -ne $SubscriptionId) {
    Invoke-Step "az account set --subscription $SubscriptionId" {
        & $azCmd account set --subscription $SubscriptionId
    }
}

$scope = "/subscriptions/$SubscriptionId"

foreach ($g in $plan) {
    Write-Host ''
    Write-Host "━━━ $($g.Name) ━━━" -ForegroundColor Magenta

    # 1) Resolve-or-create group
    $existing = & $azCmd ad group list --display-name $g.Name --query '[0].id' -o tsv 2>$null
    if ($existing) {
        Write-Host "  Group exists: id=$existing" -ForegroundColor Green
        $groupId = $existing
    } else {
        $mailNick = ($g.Name -replace '-', '').ToLowerInvariant()
        $groupId = Invoke-Step "Create group $($g.Name)" {
            & $azCmd ad group create `
                --display-name $g.Name `
                --mail-nickname $mailNick `
                --description $g.Description `
                --query 'id' -o tsv
        }
        if (-not $DryRun -and -not $groupId) {
            throw "Failed to create group $($g.Name)"
        }
        Write-Host "  Created: id=$groupId" -ForegroundColor Green
    }

    if ($DryRun) { $groupId = '<group-id-pending-create>' }

    # 2) Assign each role (idempotent — az returns existing assignment if present)
    foreach ($role in $g.Roles) {
        # Check existing (no --assignee-principal-type on `list`)
        $existingAssign = & $azCmd role assignment list `
            --assignee $groupId `
            --scope $scope `
            --role $role `
            --query '[0].id' -o tsv 2>$null
        if ($existingAssign) {
            Write-Host "    [OK] $role already assigned" -ForegroundColor DarkGreen
            continue
        }
        # Replication delay after group create can cause PrincipalNotFound — retry up to 5x.
        $attempt = 0; $maxAttempts = 5; $assigned = $false
        while (-not $assigned -and $attempt -lt $maxAttempts) {
            $attempt++
            try {
                Invoke-Step "Assign '$role' to $($g.Name) at $scope (attempt $attempt)" {
                    & $azCmd role assignment create `
                        --assignee-object-id $groupId `
                        --assignee-principal-type Group `
                        --role $role `
                        --scope $scope `
                        --query 'id' -o tsv 2>&1
                } | Out-Null
                # Verify (the error doesn't always fail the call in the right way)
                $check = & $azCmd role assignment list --assignee $groupId --scope $scope --role $role --query '[0].id' -o tsv 2>$null
                if ($check) {
                    $assigned = $true
                    Write-Host "    [+] $role" -ForegroundColor Green
                } else {
                    Write-Host "    waiting 15s for principal replication..." -ForegroundColor DarkYellow
                    Start-Sleep 15
                }
            } catch {
                Write-Host "    attempt $attempt failed: $_" -ForegroundColor DarkYellow
                Start-Sleep 15
            }
        }
        if (-not $assigned) {
            Write-Warning "Failed to assign $role to $($g.Name) after $maxAttempts attempts."
        }
    }
}

Write-Host ''
Write-Host '━━━ Verify ━━━' -ForegroundColor Magenta
foreach ($g in $plan) {
    $gid = & $azCmd ad group show --group $g.Name --query 'id' -o tsv 2>$null
    if (-not $gid) { Write-Host "  $($g.Name): NOT FOUND" -ForegroundColor Red; continue }
    Write-Host ""
    Write-Host "  $($g.Name)  (id=$gid)" -ForegroundColor Cyan
    & $azCmd role assignment list --assignee $gid --scope $scope `
        --query '[].{role:roleDefinitionName,scope:scope}' -o table
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host ''
Write-Host 'Next steps for operators of pdx-mwc-26 (enableAzureRbac=false):' -ForegroundColor Yellow
Write-Host '  Add the Contributors and Administrators groups to the AAD group AKS-Admins' -ForegroundColor Yellow
Write-Host '  (094db372-f9b2-4477-937c-869b8cf2bb8a) for kubectl access. Example:' -ForegroundColor Yellow
Write-Host '    az ad group member add --group AKS-Admins --member-id <foundry-contrib-group-id>' -ForegroundColor Yellow
