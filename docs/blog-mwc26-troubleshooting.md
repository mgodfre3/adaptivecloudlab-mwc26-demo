# When the Edge Goes Dark: Debugging a Kubernetes Networking Meltdown on Azure Local

*How a silent kube-proxy failure took down our MWC 2026 demo — and how we built a self-healing watchdog so it never happens again.*

---

We were running a live drone fleet monitoring demo at Mobile World Congress 2026. Five simulated autonomous drones patrolling Barcelona landmarks, real-time 5G telemetry streaming through Azure IoT Operations, and a Phi-4 Mini language model running inference on an NVIDIA A2 GPU — all orchestrated on a six-node AKS Arc cluster running on two Lenovo SE350 servers in Portland, Oregon.

Then the dashboard went dark.

`mwc.adaptivecloudlab.com` — the URL printed on every handout at our booth — stopped responding.

## The Setup

Our architecture runs entirely at the edge on Azure Local (formerly Azure Stack HCI):

- **6 Kubernetes nodes** — 1 control plane, 4 workers, 1 GPU node
- **MetalLB** in L2 mode providing VIPs for LoadBalancer services
- **Calico** with VXLAN overlay for pod networking
- **nginx ingress controller** behind MetalLB VIP `172.21.229.180`
- **Grafana + Prometheus** monitoring stack with NVIDIA DCGM for GPU metrics
- **Phi-4 Mini Instruct** running on the GPU node via Foundry Local Inference Operator

Since the cluster is on-premises with no public IP, we access it through Azure Arc's connected Kubernetes proxy — `az connectedk8s proxy` tunnels `kubectl` commands through Azure Resource Manager.

## Act 1: The Load Balancer Mystery

The first clue was deceptive. Everything in the cluster *looked* healthy:

```
$ kubectl get pods -n drone-demo
NAME                         READY   STATUS    RESTARTS
dashboard-75544899f-qxmjp    1/1     Running   0
simulator-5f468c9474-6lrll   1/1     Running   0

$ kubectl get ingress -n drone-demo
NAME        CLASS   HOSTS                      ADDRESS          PORTS
dashboard   nginx   mwc.adaptivecloudlab.com   172.21.229.180   80, 443
```

Pods running. Ingress configured. MetalLB VIP assigned. TLS certificate valid. All six nodes reporting Ready.

So why wasn't the site responding?

### The Canary in the Coal Mine

We tried deploying a simple debug pod to test internal connectivity:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: debug-curl
spec:
  template:
    spec:
      containers:
      - name: curl
        image: curlimages/curl:latest
        command: ["curl", "-s", "http://dashboard.drone-demo.svc:80/"]
      restartPolicy: Never
```

It never started. Stuck in `ContainerCreating` with this error:

```
Warning  FailedCreatePodSandBox  kubelet  Failed to create pod sandbox:
  plugin type="calico" failed (add): error getting ClusterInformation:
  dial tcp 10.96.0.1:443: i/o timeout
```

The Calico CNI plugin couldn't reach the Kubernetes API server at its ClusterIP (`10.96.0.1:443`). And this wasn't just one node — it was **every node in the cluster**.

### Root Cause: Silent kube-proxy Failure

The ClusterIP `10.96.0.1` is a virtual IP managed by kube-proxy's iptables rules. When a process on a node connects to `10.96.0.1:443`, kube-proxy's DNAT rule translates it to the real API server endpoint (`172.21.229.131:6443`).

Those iptables rules had silently broken.

The kube-proxy pods were still running. They passed their readiness probes. Kubernetes reported them as healthy. But the iptables rules they managed were stale or corrupted — likely from a node reboot event 39 hours earlier that cascaded through the networking stack.

This created a devastating chain reaction:

1. **kube-proxy iptables broken** → ClusterIP routing fails
2. **Calico CNI can't reach API server** → no new pods can start
3. **MetalLB VIP traffic drops** → kube-proxy can't DNAT VIP to ingress pods
4. **Site goes dark** → `mwc.adaptivecloudlab.com` stops responding

The insidious part: **existing pods kept running**. Their network namespaces were set up before the failure. Readiness probes passed. Kubernetes thought everything was fine.

### The Fix

```bash
kubectl rollout restart daemonset/kube-proxy -n kube-system
```

One command. Fresh kube-proxy pods rebuilt all iptables rules from scratch. The load balancer came back online within minutes.

## Act 2: Building the Watchdog

A demo that goes down once is a war story. A demo that goes down twice is a career event. We needed automated detection and remediation.

The challenge: when kube-proxy breaks, the Calico CNI breaks too. CronJobs can't start. Monitoring pods can't be created. The standard Kubernetes self-healing loop is severed.

### The Solution: A DaemonSet That Can't Be Killed

We built a watchdog DaemonSet with three key design decisions:

**1. `hostNetwork: true`** — The watchdog runs on the host network stack, completely bypassing the Calico CNI. Even when the overlay network is destroyed, the watchdog keeps running.

**2. Probes the exact failure path** — Every 30 seconds, it tries to TCP connect to `10.96.0.1:443` from the host. This is the same path that breaks when kube-proxy fails — not a synthetic health check, but a real test of the actual routing.

**3. Per-node surgical remediation** — When failures are detected, the watchdog deletes only the local node's kube-proxy pod. The DaemonSet controller recreates it with fresh iptables. No cluster-wide restart, no blast radius.

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: kube-proxy-watchdog
spec:
  template:
    spec:
      hostNetwork: true          # Survives CNI failure
      dnsPolicy: ClusterFirstWithHostNet
      containers:
        - name: watchdog
          image: busybox:1.36    # 5MB, no dependencies
          command: ["sh", "/scripts/watchdog.sh"]
          resources:
            requests:
              cpu: 10m
              memory: 16Mi
```

The watchdog script is straightforward: probe, count failures, remediate after a threshold, cool down to prevent restart storms. It uses the Kubernetes API directly (via ServiceAccount token) to delete the local kube-proxy pod — no `kubectl` binary needed.

Total footprint: **16MB RAM per node**. Cost of the outage it prevents: immeasurable.

## Act 3: The Calico VXLAN Cascade

With the load balancer fixed, we turned to Grafana — which was showing zero data in every tile.

This kicked off a second, deeper investigation. kube-state-metrics had been crash-looping for 39 hours (1,530 restarts). Prometheus was stranded on a node with a broken Calico agent. The VXLAN overlay network — which carries all pod-to-pod cross-node traffic — was in shambles.

The Calico `install-cni` init container was failing on 4 of 6 worker nodes with exit code 1. The CNI kubeconfig on each node pointed to the Kubernetes ClusterIP, which needed kube-proxy, creating a chicken-and-egg problem that persisted even after kube-proxy was restarted.

### The Recovery Strategy: Consolidate to the Healthy Island

Rather than fighting the overlay network, we identified the **one worker node** with a fully functional networking stack (`.136` — the GPU node, ironically) and consolidated everything there:

| Component | Before | After |
|---|---|---|
| Ingress Controller | 2 pods on .134/.135 (broken overlay) | 1 pod on .136 |
| Grafana | .133 (hostNetwork, unreachable) | .136 (pod network) |
| Prometheus | .134 (hostNetwork, unreachable) | .136 (pod network) |
| kube-state-metrics | .136 (hostNetwork) | .136 (hostNetwork) |
| DCGM GPU Exporter | — (wrong nodeSelector) | .136 (pod network) |

All monitoring traffic now stays **on-node** through the local bridge — zero dependency on cross-node VXLAN tunnels. The ingress controller and its backends share the same pod network namespace, so requests never leave the node.

One bonus discovery: the DCGM exporter DaemonSet had a stale nodeSelector from a previous cluster (`pdx-aks-ead6c2bf-pdxgpu` vs the current `pdx-mwc-26-cb393e45-pdxgpu`). GPU metrics had likely never worked on this cluster.

## Lessons Learned

### 1. kube-proxy Can Fail Silently

kube-proxy's health probes check if the process is running, not if its iptables rules are correct. A "healthy" kube-proxy pod can have completely broken routing. If you run on bare metal with MetalLB, consider adding ClusterIP reachability probes to your monitoring.

### 2. The CNI Is Your Single Point of Failure

When the CNI breaks, Kubernetes loses its ability to self-heal. New pods can't start. Monitoring can't deploy. CronJobs can't run. Your watchdogs need to run on `hostNetwork` to survive this failure mode.

### 3. Edge Clusters Need Different Operational Patterns

In the cloud, you'd just replace the node. At the edge, your nodes are physical servers in a data center (or under a desk at a trade show). You need:

- **Self-healing DaemonSets** that survive networking failures
- **Co-location strategies** that minimize cross-node dependencies
- **Arc proxy access** as a reliable control plane path when the data plane is down

### 4. `hostNetwork` Is Your Emergency Escape Hatch

When the overlay network is burning, `hostNetwork: true` lets pods communicate directly over the physical network. It's not a permanent architecture — but it's an invaluable triage tool.

### 5. Force-Deleting Pods With PVCs Has Consequences

We learned the hard way that `kubectl delete pod --force` on a pod with a CSI-backed PVC doesn't cleanly detach the volume. The `VolumeAttachment` resource lingers, causing `Multi-Attach` errors when the pod reschedules. Always clean up `VolumeAttachment` resources after force-deleting stateful pods.

## The Result

By the end of the day:

- ✅ `mwc.adaptivecloudlab.com` — live dashboard serving drone telemetry
- ✅ `grafana.adaptivecloudlab.com` — full monitoring with GPU metrics
- ✅ Phi-4 Mini running inference on the NVIDIA A2 GPU
- ✅ Self-healing watchdog deployed across all nodes
- ✅ Everything consolidated on the one healthy node with working Calico

The demo ran for the rest of the conference without interruption.

---

*The drone monitoring demo, kube-proxy watchdog, and all infrastructure code are open source at [adaptivecloudlab-mwc26-demo](https://github.com/mgodfre3/adaptivecloudlab-mwc26-demo).*
