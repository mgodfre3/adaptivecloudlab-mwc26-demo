# Fabric + Power BI Setup Guide

## Overview

This directory contains everything needed to connect **Microsoft Fabric** and **Power BI** to the drone fleet demo. Office-based staff use these reports to review fleet-wide 5G network quality across all deployments — without ever seeing exact GPS coordinates or raw device telemetry.

```
[Azure Local — Field]          [Azure Cloud — Office]
 Drone Simulator                Microsoft Fabric Workspace
   → AIO MQTT                     ← IoT Hub Eventstream
   → IoT Hub ─────────────────→      Eventhouse (KQL DB)
                                           ↓
                                   Power BI Semantic Model
                                           ↓
                                   Power BI Report / Dashboard
```

The AIO Dataflow (`k8s/iot-ops-dataflow.yaml`) already anonymises telemetry before it reaches IoT Hub — GPS is rounded to ~1.1 km area grids, altitude and device metadata are stripped. Fabric and Power BI only ever see this sanitised data.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Microsoft Fabric capacity | F2 or above (or trial) |
| IoT Hub | Already deployed by `scripts/02-deploy-iot-hub.ps1` |
| AIO Dataflow active | `k8s/iot-ops-dataflow.yaml` applied to the cluster |
| Power BI Desktop | Required to open/publish `powerbi-report.pbit` |

---

## Step 1 — Create the Fabric Workspace

1. Go to [app.fabric.microsoft.com](https://app.fabric.microsoft.com)
2. Click **Workspaces** → **New workspace**
3. Name: `AdaptiveCloudLab-FleetOps`
4. Assign to a Fabric capacity (not shared / Pro)
5. Click **Apply**

---

## Step 2 — Create the Eventhouse

1. Inside the workspace, click **New item** → **Eventhouse**
2. Name: `DroneFleetEH`
3. A KQL database named `DroneFleetDB` is created automatically

Note the **Query URI** shown in the Eventhouse overview — you will need it for Power BI Desktop. It looks like:
```
https://<workspace-id>.kusto.fabric.microsoft.com
```

---

## Step 3 — Apply the KQL Schema

1. In the Eventhouse, open the **KQL Queryset**
2. Copy and run the contents of `kql-schema.kql` — this creates:
   - `DroneMetrics` table with the correct column types
   - JSON ingestion mapping `DroneMetricsMapping`
   - 90-day retention policy
3. Copy and run the contents of `kql-views.kql` — this creates:
   - `DroneMetrics_Hourly` materialized view (pre-aggregated per hour)
   - KQL functions used by Power BI: `FleetHealthKPI`, `SignalTrend24h`, `DroneStatusDistribution`, `AreaSignalHeatmap`, `AnomalyEvents`, `PacketLossTrend`

---

## Step 4 — Create the Fabric Eventstream

1. In the workspace, click **New item** → **Eventstream**
2. Name: `DroneFleetStream`
3. **Add source** → **Azure IoT Hub**
   - Select your subscription and IoT Hub (e.g. `pdx-iothub` — use the hub deployed by `scripts/02-deploy-iot-hub.ps1`)
   - Consumer group: create a new one named `fabric-ingest`
   - Data format: **JSON**
4. **Add destination** → **KQL Database (Eventhouse)**
   - Select `DroneFleetDB` / table `DroneMetrics`
   - Ingestion mapping: `DroneMetricsMapping`
5. Click **Publish** to start the stream

> **Tip:** Use the Eventstream **Data preview** tab to verify JSON messages are flowing before connecting Power BI.

---

## Step 5 — Open and Configure the Power BI Template

1. Open **Power BI Desktop**
2. File → **Open** → select `powerbi-report.pbit`
3. When prompted for the **FabricKQLCluster** parameter, enter your Query URI:
   ```
   https://<workspace-id>.kusto.fabric.microsoft.com
   ```
4. Click **Load** — Power BI will authenticate with your Fabric identity and pull data
5. Verify all three report pages load correctly (see [Report Pages](#report-pages) below)

---

## Step 6 — Publish to Fabric

1. In Power BI Desktop, click **Publish**
2. Select the `AdaptiveCloudLab-FleetOps` workspace
3. The report and semantic model will appear in the workspace
4. Set a **scheduled refresh** if needed (the Eventhouse provides near-real-time data via DirectQuery or short import cycles)

---

## Report Pages

### Page 1 — Fleet Health Overview (executive view)

| Visual | Data Source | Description |
|---|---|---|
| KPI — Active Drones | `FleetHealthKPI()` | Distinct drones seen in the last hour |
| KPI — Avg RSRP | `FleetHealthKPI()` | Fleet-average signal strength (dBm) |
| KPI — Avg DL Throughput | `FleetHealthKPI()` | Fleet-average downlink (Mbps) |
| KPI — Avg Latency | `FleetHealthKPI()` | Fleet-average round-trip latency (ms) |
| Line chart — Signal Trend | `SignalTrend24h()` | RSRP / SINR / DL throughput over last 24 h |
| Donut — Status Distribution | `DroneStatusDistribution()` | Drones by lifecycle state (patrolling / returning / charging …) |

### Page 2 — Network Quality Map

| Visual | Data Source | Description |
|---|---|---|
| Map (Azure Maps visual) | `AreaSignalHeatmap()` | Avg RSRP colour-coded by ~1.1 km area grid cell |
| RSRP filter slicer | `AreaSignalHeatmap()` | Narrow map to specific signal quality bands |

> **Privacy note:** Area coordinates are pre-rounded by the AIO Dataflow — exact drone GPS never reaches Fabric or Power BI.

### Page 3 — Anomaly & Alert History

| Visual | Data Source | Description |
|---|---|---|
| Table — Anomaly Events | `AnomalyEvents()` | Last 200 readings breaching: latency > 15 ms, packet loss > 1%, RSRP < −100 dBm |
| Line chart — Packet Loss | `PacketLossTrend()` | Avg and max packet loss % per hour (last 24 h) |

---

## Embedded Analytics (Optional)

The dashboard supports an `/analytics` route that embeds a published Power BI report in-browser. Set these environment variables to enable it:

```env
POWERBI_EMBED_ENABLED=true
POWERBI_REPORT_URL=https://app.powerbi.com/reportEmbed?reportId=<id>&groupId=<workspace-id>
POWERBI_WORKSPACE_ID=<workspace-id>
POWERBI_REPORT_ID=<report-id>
```

Navigate to `https://mwc.adaptivecloudlab.com/analytics` to see both the edge view and the office BI view on the same screen.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Eventstream shows no data | Verify AIO Dataflow is running: `kubectl get dataflow -n azure-iot-operations` |
| Power BI can't connect | Ensure your Entra ID account has **Viewer** or above on the Fabric workspace |
| `FleetHealthKPI()` returns empty | Check that DroneMetrics table has rows: run `DroneMetrics \| count` in KQL Queryset |
| Map visual shows no points | Confirm `area_lat` / `area_lon` columns are `real` type, not `string` |
| `.pbit` parameter prompt not shown | Open in Power BI Desktop ≥ November 2024 |
