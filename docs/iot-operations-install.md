# Azure IoT Operations Installation on ACX-HMI-26

Step-by-step commands used to install Azure IoT Operations on the **ACX-HMI-26** AKS Arc cluster.

## Cluster Details

| Detail | Value |
|---|---|
| **Cluster Name** | `ACX-HMI-26` |
| **Resource Group** | `ACX-HMI-26` |
| **Region** | `southcentralus` |
| **K8s Version** | `1.32.6` |
| **Nodes** | 2 (1 control plane + 1 worker) |

---

## Prerequisites

- Azure CLI 2.60+ with extensions: `azure-iot-ops`, `connectedk8s`, `k8s-extension`
- Helm 3.12+
- kubectl 1.28+ with cluster access
- An Arc-connected Kubernetes cluster

---

## Step 1: Connect to the Cluster

```powershell
az connectedk8s proxy --name ACX-HMI-26 --resource-group ACX-HMI-26
```

Verify connectivity:

```powershell
kubectl get nodes
```

---

## Step 2: Install cert-manager v1.19.2

Required by IoT Operations for TLS certificate management.

```powershell
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update jetstack

helm install cert-manager jetstack/cert-manager \
    --namespace cert-manager \
    --create-namespace \
    --version v1.19.2 \
    --set crds.enabled=true \
    --wait --timeout 5m
```

---

## Step 3: Install trust-manager v0.20.3

Required by IoT Operations for distributing CA trust bundles.

```powershell
helm install trust-manager jetstack/trust-manager \
    --namespace cert-manager \
    --version v0.20.3 \
    --wait --timeout 5m
```

---

## Step 4: Install Azure CLI Extensions

```powershell
# Optional: redirect extension dir on restricted hosts
$env:AZURE_EXTENSION_DIR = "$env:TEMP\az_extensions"

az extension add --name azure-iot-ops --upgrade --yes
az extension add --name connectedk8s --upgrade --yes
az extension add --name k8s-extension --upgrade --yes
```

---

## Step 5: Initialize IoT Operations on the Cluster

Installs the secret-store extension and prepares the cluster for IoT Operations.

```powershell
az iot ops init \
    --cluster ACX-HMI-26 \
    -g ACX-HMI-26 \
    --user-trust \
    --no-preflight \
    --no-progress
```

> **Note:** `--user-trust` skips deploying a system cert-manager (we installed our own in Step 2).  
> `--no-preflight` skips cluster health validation (needed if Arc Resource Health reports stale "Degraded" status despite healthy pods).

---

## Step 6: Create Storage Account (ADLS Gen2)

Required by the schema registry for storing schemas.

```powershell
az storage account create \
    --name acxhmi26iotsa \
    -g ACX-HMI-26 \
    --location southcentralus \
    --sku Standard_LRS \
    --kind StorageV2 \
    --enable-hierarchical-namespace true
```

---

## Step 7: Create Device Registry Namespace

```powershell
az iot ops ns create \
    -n acxhmi26-ns \
    -g ACX-HMI-26 \
    --location southcentralus
```

---

## Step 8: Create Schema Registry

```powershell
# Get the storage account resource ID
SA_ID=$(az storage account show --name acxhmi26iotsa -g ACX-HMI-26 --query "id" -o tsv)

az iot ops schema registry create \
    --name acxhmi26-sr \
    --rn acxhmi26-ns \
    -g ACX-HMI-26 \
    --sa-resource-id $SA_ID \
    --location southcentralus
```

---

## Step 9: Create Trust Infrastructure

Self-signed CA chain for IoT Operations TLS.

### 9a. Self-Signed ClusterIssuer (Root)

```yaml
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: iot-ops-selfsigned
spec:
  selfSigned: {}
EOF
```

### 9b. Root CA Certificate

```yaml
kubectl apply -f - <<EOF
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
EOF
```

### 9c. CA ClusterIssuer (Signs Leaf Certs)

```yaml
kubectl apply -f - <<EOF
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: iot-ops-ca-issuer
spec:
  ca:
    secretName: iot-ops-root-ca-secret
EOF
```

### 9d. Trust Bundle (Distributes CA Cert as ConfigMap)

```yaml
kubectl apply -f - <<EOF
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
EOF
```

---

## Step 10: Patch az CLI Extension for `southcentralus` (Required)

> **⚠️ Important:** In `southcentralus`, the IoT Operations extension only supports the `preview` release train, not `stable`. The az CLI extension must be patched before running `az iot ops create`.

Locate and edit the template file:

```
$env:AZURE_EXTENSION_DIR\azure-iot-ops\azext_edge\edge\providers\orchestration\template.py
```

### Changes required (in the `create` template, around line 1246–1311):

1. **Change release train** from `stable` → `preview`:
   ```python
   # Find this line:
   "TRAINS": {"iotOperations": "stable"},
   # Change to:
   "TRAINS": {"iotOperations": "preview"},
   ```

2. **Remove the version pin** — delete the `"version"` line from the iotOperations extension resource:
   ```python
   # Remove this line entirely:
   "version": "[coalesce(tryGet(tryGet(parameters('advancedConfig'), 'aio'), 'version'), variables('VERSIONS').iotOperations)]",
   ```

3. **Enable auto-upgrade** — change `autoUpgradeMinorVersion` from `False` to `True`:
   ```python
   # Find (in the iotOperations extension section):
   "autoUpgradeMinorVersion": False,
   # Change to:
   "autoUpgradeMinorVersion": True,
   ```

The final iotOperations extension block should look like:
```python
"extensionType": "microsoft.iotoperations",
"releaseTrain": "[coalesce(tryGet(...), variables('TRAINS').iotOperations)]",
"autoUpgradeMinorVersion": True,
```

---

## Step 11: Create IoT Operations Instance

This is the main deployment — takes ~10-20 minutes.

```powershell
# Get resource IDs
SR_ID=$(az iot ops schema registry show --name acxhmi26-sr -g ACX-HMI-26 --query "id" -o tsv)
NS_ID=$(az iot ops ns show -n acxhmi26-ns -g ACX-HMI-26 --query "id" -o tsv)

az iot ops create \
    --cluster ACX-HMI-26 \
    -g ACX-HMI-26 \
    --name acxhmi26-iot-ops \
    --sr-resource-id $SR_ID \
    --ns-resource-id $NS_ID \
    --add-insecure-listener \
    --no-progress \
    --no-preflight \
    --trust-settings \
        configMapName=iot-ops-trust-bundle \
        configMapKey=trust-bundle.pem \
        issuerKind=ClusterIssuer \
        issuerName=iot-ops-ca-issuer
```

> **Note:** `--add-insecure-listener` creates a non-TLS MQTT listener on port 1883 for easy development/testing.
>
> **Note:** `--no-preflight` skips cluster health validation (needed if Arc Resource Health reports stale "Degraded" status).
>
> **Tip:** Stop the `az connectedk8s proxy` before running this command to avoid CLI token timeout conflicts. The `az iot ops create` command talks to ARM and does not need kubectl access.

---

## Verification

### Check IoT Operations status

```powershell
az iot ops show --cluster ACX-HMI-26 -g ACX-HMI-26
```

### Check pods in azure-iot-operations namespace

```powershell
kubectl get pods -n azure-iot-operations
```

### Check the MQTT broker service

```powershell
kubectl get svc -n azure-iot-operations | grep broker
```

---

## Azure Resources Created

| Resource | Name | Type |
|---|---|---|
| Storage Account | `acxhmi26iotsa` | ADLS Gen2 (Standard_LRS) |
| Device Registry Namespace | `acxhmi26-ns` | Microsoft.DeviceRegistry/namespaces |
| Schema Registry | `acxhmi26-sr` | Microsoft.DeviceRegistry/schemaRegistries |
| IoT Operations Instance | `acxhmi26-iot-ops` | Microsoft.IoTOperations |

## Kubernetes Resources Created

| Resource | Namespace | Purpose |
|---|---|---|
| cert-manager | `cert-manager` | TLS certificate lifecycle management |
| trust-manager | `cert-manager` | CA trust bundle distribution |
| ClusterIssuer `iot-ops-selfsigned` | cluster-scoped | Self-signed root issuer |
| Certificate `iot-ops-root-ca` | `cert-manager` | Root CA certificate |
| ClusterIssuer `iot-ops-ca-issuer` | cluster-scoped | CA issuer for leaf certs |
| Bundle `iot-ops-trust-bundle` | cluster-scoped | Distributes CA as ConfigMap |
| Azure IoT Operations | `azure-iot-operations` | MQTT broker, dataflows, device registry |

---

## Cleanup

To remove IoT Operations from the cluster:

```powershell
# Delete the IoT Operations instance
az iot ops delete --cluster ACX-HMI-26 -g ACX-HMI-26 --name acxhmi26-iot-ops --yes

# Delete Azure resources
az iot ops schema registry delete --name acxhmi26-sr -g ACX-HMI-26 --yes
az iot ops ns delete -n acxhmi26-ns -g ACX-HMI-26 --yes
az storage account delete --name acxhmi26iotsa -g ACX-HMI-26 --yes

# Delete trust infrastructure
kubectl delete bundle iot-ops-trust-bundle
kubectl delete clusterissuer iot-ops-ca-issuer iot-ops-selfsigned
kubectl delete certificate iot-ops-root-ca -n cert-manager

# Uninstall Helm charts
helm uninstall trust-manager -n cert-manager
helm uninstall cert-manager -n cert-manager
kubectl delete namespace cert-manager
```
