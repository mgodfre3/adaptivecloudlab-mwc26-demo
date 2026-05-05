# Demo Talking Points — The Office View
## Power BI + Microsoft Fabric Layer

**Audience:** Fleet managers, operations leads, executives  
**Duration:** 2 minutes (Act 4 of the full 10-minute demo)  
**Setup:** Power BI report open in a browser tab alongside the edge dashboard

---

## Context Switch

> *"Everything we just saw on the Drone Network Monitor — the live map, the AI insights, the real-time signal bars — that's the field view. It runs on two physical servers in the same room, and nothing sensitive leaves those servers.*
>
> *Now let me show you what the office sees."*

Switch browser tabs to the Power BI report.

---

## Page 1 — Fleet Health Overview

Point to the KPI cards:

> *"These four numbers give an operations manager an instant read on fleet health — how many drones are active right now, what the average signal quality looks like across the whole fleet, what throughput we're getting, and whether latency is within SLA.*
>
> *This isn't a snapshot someone emailed over. It's pulling live aggregated data from Microsoft Fabric — the same IoT Hub stream that feeds the edge dashboard is also feeding this report, in real time."*

Point to the signal trend chart:

> *"This line goes back 24 hours. If there was a degradation event last night, the manager sees it here without having to ask the field team. Trend data, not just the current moment."*

Point to the donut chart:

> *"And this shows where the fleet's time goes — how many drones are actively patrolling versus returning to charge. Useful for capacity planning and shift scheduling."*

---

## Page 2 — Network Quality Map

> *"Here's the same geographic view — but notice what's different. On the edge dashboard, you see every drone's exact GPS position to six decimal places. Here, GPS is rounded to area-level grids — about a 1.1 kilometre square.*
>
> *That rounding happens automatically in the IoT Operations dataflow before data ever leaves the field hardware. The office team gets the network quality heat map — strong signal in green, weak in red — but they cannot reverse-engineer exact drone positions. That's data sovereignty by design, not by policy document."*

Point to colours:

> *"Green areas have strong 5G coverage — high RSRP, low latency. Red areas are dead zones or interference. Over time this map builds a coverage intelligence layer that ops teams can use to route drones more efficiently or flag areas needing infrastructure investment."*

---

## Page 3 — Anomaly & Alert History

> *"The last page is for the operations team reviewing incidents. Any time a drone experiences latency above 15 milliseconds, packet loss above 1%, or signal below minus 100 dBm, it appears in this table — timestamped, with the drone identifier and which thresholds were breached.*
>
> *This is the audit trail that IoT teams, network engineers, and compliance teams need. The edge dashboard shows you the problem in real time. This page tells you how often it happens, when it happened, and whether it's getting better or worse."*

Point to the packet loss trend chart:

> *"This trend line is the one I'd keep on a NOC screen. A spike here means something changed in the RF environment — interference, a cell tower issue, weather. Correlate it with the time on page one and you have a starting point for root cause analysis."*

---

## Closing Statement for This Section

> *"So here's the architecture in one sentence: Azure Local handles the field — real-time, private, resilient, no cloud dependency. Fabric and Power BI handle the boardroom — aggregated, accessible, persistent, beautiful. The same IoT Hub that feeds the edge dashboard feeds the enterprise BI layer. One pipeline, two personas, appropriate data at each layer.*
>
> *The field team gets exact intelligence. The office gets the summary they need to make decisions. And sensitive operational data never goes further than it has to."*

---

## Key Messages (repeat if needed)

| Message | When to use |
|---|---|
| "One pipeline, two personas" | Connecting edge → cloud story |
| "Rounded at the source, not at the report" | When asked about privacy / GDPR |
| "24-hour trend, not just the current moment" | When comparing to edge dashboard |
| "Audit trail for compliance" | When talking to ops/legal audience |
| "No cloud compute at the edge" | When asked about cost or resilience |

---

## Handling Questions

**Q: Can the office team see individual drone positions?**  
A: No. The AIO Dataflow rounds GPS to a 1.1 km grid before export. The Power BI report only receives area-level coordinates — by the time data reaches Fabric, exact positions are already gone.

**Q: How fresh is the data in Power BI?**  
A: The Fabric Eventstream ingests from IoT Hub continuously. The Eventhouse materialized view (`DroneMetrics_Hourly`) updates as data arrives. Power BI with DirectQuery against Fabric KQL delivers sub-minute latency.

**Q: What if IoT Hub goes down?**  
A: The edge dashboard keeps running — it uses AIO MQTT on-cluster and doesn't depend on IoT Hub for the live view. The Power BI report would stop updating, but the edge operations continue unaffected.

**Q: Can we add more cities?**  
A: Yes. Each field deployment sends data to the same IoT Hub. The `DroneMetrics` table accumulates data from all deployments. Add a city slicer to Page 2 and you have a multi-site network quality map with no additional infrastructure.

**Q: What does this cost?**  
A: The main cost is the Fabric capacity (F2 ≈ $250/month or use trial). IoT Hub message ingestion is already paid for the edge demo. No additional Azure compute is needed — Fabric Eventhouse is serverless.
