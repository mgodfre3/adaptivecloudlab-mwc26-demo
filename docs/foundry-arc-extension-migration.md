# Migrating Foundry Local from Helm → Arc Extension

**Scope:** Switch both Portland Azure Local stamp AKS Arc clusters from the
private-preview Helm chart deployment to the public-preview **`Microsoft.Foundry`**
Azure Arc Kubernetes extension.

| Cluster | Resource Group | Subscription | Current state |
|---|---|---|---|
| `pdx-mwc-26` | `pdx-rg` | `fbaf508b-cb61-4383-9cda-a42bfa0c7bc9` | Helm OCI chart `inferenceoperator@0.0.1-prp.5` in `pdx-foundry-op` |
| `vi-portland` | `pdx-rg` | `fbaf508b-cb61-4383-9cda-a42bfa0c7bc9` | Helm OCI chart `inferenceoperator@0.0.1-prp.5` in `vipdx-foundry-op` |

Both clusters share the same subscription, RG, custom location (`portland`),
and VNet (`pdx-lnet-vlan31`) — so any subscription-scoped preview enrollment
covers both. Both are already Arc `connectedClusters` (k8s 1.32) which is the
prerequisite cluster type for `Microsoft.Foundry`.

---

## What actually changes

### What stays the same
- CRDs: extension installs the **same** `foundrylocal.azure.com/v1` `Model` and
  `ModelDeployment` kinds we already use. Our existing manifests in
  `k8s/foundry-local.yaml` and `k8s/vi-foundry-local.yaml` are reusable with
  one edit (namespace).
- Inference data path: still in-cluster, OpenAI-compatible, GPU scheduled the
  same way. Phi-4-mini catalog model still available.
- NGINX ingress, MetalLB, Longhorn, Video Indexer extension — untouched.

### What has to change
1. **Replace jetstack `cert-manager` + `trust-manager` Helm releases** with the
   `Microsoft.CertManagement` Arc extension (the only supported prereq for
   `Microsoft.Foundry`). Our current trust-manager flags
   (`defaultPackage.enabled=false`, `secretTargets.enabled=true`) map 1:1 to
   the extension config keys.
2. **Replace the OCI Helm `inferenceoperator` chart** with
   `az k8s-extension create --extension-type Microsoft.Foundry`.
3. **Operator namespace becomes fixed at `foundry-local-operator`** (the
   extension's `--release-namespace`). Our per-prefix namespaces
   (`pdx-foundry-op`, `vipdx-foundry-op`, `pdx-foundry-mdl`,
   `vipdx-foundry-mdl`) go away. Both clusters end up with the same
   namespace name — that's fine because they're separate kube API servers.
4. **Auth flips from API key → Microsoft Entra ID JWT.** The extension does
   not accept the Helm chart's `--set apiKey=...` mode. We need an
   **app registration** (one is enough — used by both clusters) and to pass
   `--config entraAuth.tenantId=... --config entraAuth.clientId=...`.
5. **Consumers re-point to the new endpoint host.** Anywhere we reference
   `phi-4-deployment.<prefix>-foundry-mdl.svc:5000` or
   `phi-4-deployment.vi-foundry-local.svc:5000` flips to
   `phi-4-deployment.foundry-local-operator.svc:5000`.

### What we lose (acceptable for the demo)
- Per-cluster custom operator namespace (cosmetic only).
- API-key auth (we replace with Entra; the drone and dashboard already use
  an env var, so it's a token-source swap not a code change).
- The private-preview chart's quirky CA-bundle hand-stitching in
  `scripts/02-install-platform.ps1` lines 398-419 — the extension does this
  itself and we can delete that block.

### What we gain
- Supported, GA-track install path (no more "may 404" warning in step 4).
- `--auto-upgrade-minor-version true` — Arc handles operator upgrades.
- Multi-node + vLLM runtime become available without a chart swap (see
  Build 2026 announcement).
- Single `az k8s-extension show` for health instead of `helm status` +
  CA-bundle sanity checks.

---

## Prerequisites (one-time, per subscription)

1. **Request preview access** at <https://aka.ms/FoundryLocalAzure_PreviewRequest>
   for subscription `fbaf508b-cb61-4383-9cda-a42bfa0c7bc9`. Same form,
   single request covers both clusters.
2. **Create one app registration** for Entra ID auth (e.g. `pdx-foundry-local-auth`).
   Capture `tenantId` (already 72f988bf-... for our tenant) and the
   app's `clientId`. Store the clientId in Key Vault alongside the
   existing `*-foundry-key` secret. (No client secret is needed — the
   operator accepts bearer tokens minted by Entra.)
3. **Verify region.** Portland metadata region is `southcentralus` —
   confirmed in the [supported region list][src-regions] for
   `Microsoft.Foundry` (18 regions including South Central US).
4. **Confirm Azure CLI extensions.** Both `connectedk8s` and `k8s-extension`
   are already required by `scripts/01-create-cluster.ps1:126`, so no new
   tooling.

[src-regions]: https://learn.microsoft.com/en-us/azure/azure-sovereign-clouds/private/foundry-local/deploy-foundry-local-arc-extension

---

## Migration sequence (per cluster — run twice)

Set per-cluster variables once at the top of the session:

```powershell
# pdx-mwc-26 run
$rg = "pdx-rg"; $cluster = "pdx-mwc-26"
$tenantId = "72f988bf-86f1-41af-91ab-2d7cd011db47"
$clientId = "<app-registration-client-id>"

# vi-portland run
$rg = "pdx-rg"; $cluster = "vi-portland"
# (same tenantId / clientId)
```

### Step 1 — Drain current Foundry workloads

```powershell
# Save current CRs first so we can re-apply after the operator move
kubectl get models.foundrylocal.azure.com,modeldeployments.foundrylocal.azure.com -A -o yaml > foundry-crs-backup-$cluster.yaml

# Delete CRs (NOT CRDs — they're shared with the new operator)
kubectl delete modeldeployment phi-4-deployment -n <current-foundry-mdl-ns> --ignore-not-found
kubectl delete model phi-4-mini -n <current-foundry-mdl-ns> --ignore-not-found
```

### Step 2 — Uninstall the Helm operator (but keep CRDs)

```powershell
helm uninstall inference-operator -n <current-foundry-op-ns>
# Sanity: CRDs must remain — the new extension expects them.
kubectl get crd | Select-String foundrylocal.azure.com
```

If `helm uninstall` removes the CRDs (Helm v3 default for chart-owned CRDs
is to leave them, but the prp chart may behave differently), let the
extension reinstall them in step 4 — they are functionally identical.

### Step 3 — Replace jetstack cert/trust-manager with the Arc extension

```powershell
helm uninstall trust-manager -n cert-manager
helm uninstall cert-manager -n cert-manager
# Leave the cert-manager namespace; the new extension reuses it.

az k8s-extension create `
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
```

Wait for `Succeeded`:

```powershell
az k8s-extension show -g $rg --cluster-name $cluster `
  --cluster-type connectedClusters --name azure-cert-manager `
  --query "provisioningState" -o tsv
```

### Step 4 — Install the Foundry Arc extension

```powershell
az k8s-extension create `
  --resource-group $rg `
  --cluster-name $cluster `
  --cluster-type connectedClusters `
  --name inference-operator `
  --extension-type Microsoft.Foundry `
  --scope cluster `
  --release-namespace foundry-local-operator `
  --auto-upgrade-minor-version true `
  --release-train stable `
  --config entraAuth.tenantId=$tenantId `
  --config entraAuth.clientId=$clientId
```

Verify:

```powershell
kubectl get pods -n foundry-local-operator
kubectl get crd | Select-String foundrylocal.azure.com
```

### Step 5 — Reapply Model/ModelDeployment CRs in the new namespace

Edit `k8s/foundry-local.yaml` and `k8s/vi-foundry-local.yaml` so both
`Model.metadata.namespace` and `ModelDeployment.metadata.namespace` are
`foundry-local-operator`. (Or apply the edited copies via gitops/Flux —
see the gitops note below.) Then:

```powershell
kubectl apply -f k8s/foundry-local.yaml      # on pdx-mwc-26
kubectl apply -f k8s/vi-foundry-local.yaml   # on vi-portland
```

Watch the deployment come up (first pull of `phi-4-mini` from the catalog
takes 3-5 min on the A2):

```powershell
kubectl get modeldeployment -n foundry-local-operator -w
```

### Step 6 — Update consumer endpoints

| File | Current value | New value |
|---|---|---|
| `config/pdx-mwc-26.env` | `FOUNDRY_OPERATOR_NAMESPACE=pdx-foundry-op` | delete (or set to `foundry-local-operator`) |
| `config/pdx-mwc-26.env` | `FOUNDRY_MODEL_NAMESPACE=pdx-foundry-mdl` | delete (or set to `foundry-local-operator`) |
| `config/vi-portland.env` | `EDGE_AI_ENDPOINT=https://phi-4-deployment.vipdx-foundry-mdl.svc:5000` | `https://phi-4-deployment.foundry-local-operator.svc:5000` |
| `k8s/video-dashboard.yaml:32` | `https://phi-4-deployment.vi-foundry-local.svc:5000` | `https://phi-4-deployment.foundry-local-operator.svc:5000` |
| `k8s/drone-demo.yaml` (rendered from template via `${EDGE_AI_ENDPOINT}`) | env-derived | re-render after env update |
| `k8s/grafana-dashboard.json:330` | `namespace="foundry-local"` | `namespace="foundry-local-operator"` |
| `k8s/foundry-local.yaml` | `namespace: foundry-local` (lines 13, 26, 44) | `namespace: foundry-local-operator` (or drop the Namespace block — the extension creates it) |
| `k8s/vi-foundry-local.yaml` | `namespace: vi-foundry-local` (lines 11, 25, 43) | `namespace: foundry-local-operator` |

The drone demo and video dashboard consumers also need their **bearer token
source** updated. Today they read `FOUNDRY_API_KEY` from the secret created
in `scripts/00-bootstrap-secrets.ps1`. Replace with an Entra token mint
(workload-identity or service-principal client-credentials flow against
the new app registration) and inject as `Authorization: Bearer <token>`
instead of `api-key: <key>`. **This is the one non-trivial code change in
the migration** — everything else is config or YAML.

### Step 7 — Update `scripts/02-install-platform.ps1`

Strip the Helm install paths and the manual CA-bundle stitching:

- Delete or `#`-comment the `cert-manager` Helm install block (lines ~252-267).
- Delete or comment the `trust-manager` Helm install block (lines ~290-301).
- Replace the Foundry Helm install (lines ~310-422) with the two
  `az k8s-extension create` calls above.
- Delete the manual CA-bundle creation (lines ~398-419) — handled by the
  extension.
- Remove `helm` from the required-tools check on line 152 (kubectl is enough
  once the chart is gone; helm is still needed for ingress-nginx and Longhorn
  unless those are also moved to extensions — out of scope here).
- Drop `FOUNDRY_OPERATOR_NAMESPACE` / `FOUNDRY_MODEL_NAMESPACE` env reads
  (lines 115-116) since the namespace is now fixed.

---

## GitOps option (if we want to keep the manifest-driven path)

The CRs (`Model`, `ModelDeployment`) are still plain Kubernetes manifests.
They can live under `gitops/apps/foundry-local/` with a Flux Kustomization
pointing each cluster's `flux-system` at the right overlay. The extension
itself is *not* GitOps-managed — it's installed by `az k8s-extension create`
and lifecycle-managed by ARM. That's actually a cleaner separation: ARM
owns the operator, Flux owns the model declarations.

---

## Risk assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Preview access request takes days | Medium | Submit immediately; rollback plan = keep current Helm install live until extension `provisioningState=Succeeded` |
| Entra token plumbing in drone-demo / video-dashboard is more work than expected | Medium | Spike on `vi-portland` first (smaller blast radius); if blocked, keep API-key mode by NOT moving until the extension chart adds API-key fallback |
| `cert-manager` Helm uninstall breaks NGINX ingress TLS certs | Low | The Arc cert-manager extension provisions the same CRDs (`Certificate`, `Issuer`); existing `Certificate` objects re-reconcile after extension install |
| New operator namespace conflict | Low | Fresh namespace — no collision |
| Phi-4-mini model re-download on first pull (3-5 min) | Low | Schedule maintenance window; both clusters cache after first pull |
| CRD version skew (Helm prp.5 vs extension stable) | Low/Medium | Same API group/version (`foundrylocal.azure.com/v1`); existing CRs should re-apply cleanly. Backup taken in Step 1. |

---

## Recommended order

1. **Submit preview access request now** (blocker, days of lead time).
2. **Create the app registration** (15 min).
3. **Pilot on `vi-portland` first** — it's the smaller "backup demo" cluster,
   lower blast radius if something breaks (`config/vi-portland.env:3`
   already labels it "Backup Demo").
4. **Smoke-test the video-dashboard Entra-token path** end-to-end on vi-portland.
5. **Repeat on `pdx-mwc-26`.**
6. **Clean up `scripts/02-install-platform.ps1`** and check in.
7. **Delete `inference-operator-0.0.1-prp.5.tgz`** from the repo root once
   both clusters are migrated.

---

## Effort estimate

- Pre-work (preview access + app reg): **0.5 day** elapsed (mostly waiting).
- Migration script + manifest edits: **~3-4 hours** of focused work.
- Entra token plumbing in consumer apps: **~half a day**, including local
  test against vi-portland.
- Per-cluster cutover window (drain + extension install + CR re-apply +
  model pull + smoke test): **~45 min** each.
- Documentation + cleanup: **~1 hour**.

**Total realistic: 1.5-2 days of engineering once preview access is granted.**
