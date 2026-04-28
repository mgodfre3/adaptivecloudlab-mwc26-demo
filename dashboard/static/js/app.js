/* ═══════════════════════════════════════════════════════════════════════════
   Drone Network Monitor – Frontend Logic
   MWC 2026 • Leaflet map + Socket.IO real-time updates
   ═══════════════════════════════════════════════════════════════════════════ */

(function () {
  "use strict";

  // ── Constants ──────────────────────────────────────────────────────────
  const _mapCfg = window.DEMO_MAP_CONFIG || {};
  const MAP_CENTER = _mapCfg.center || [39.7484, -104.9951];  // Denver, CO default
  const ZOOM = _mapCfg.zoom || 13;
  const TRAIL_LEN = 120;           // max trail points per drone
  const CARD_PULSE_MS = 600;       // card highlight flash duration

  // ── Colour helpers ─────────────────────────────────────────────────────
  function rsrpColor(rsrp) {
    if (rsrp >= -80)  return "#22c55e";      // good
    if (rsrp >= -100) return "#f59e0b";      // ok
    return "#ef4444";                         // poor
  }
  function rsrpClass(rsrp) {
    if (rsrp >= -80)  return "sig-good";
    if (rsrp >= -100) return "sig-ok";
    return "sig-poor";
  }
  function batteryColor(pct) {
    if (pct > 50) return "#22c55e";
    if (pct > 20) return "#f59e0b";
    return "#ef4444";
  }

  // ── Leaflet setup ─────────────────────────────────────────────────────
  const map = L.map("map", {
    center: MAP_CENTER,
    zoom: ZOOM,
    zoomControl: false,
    attributionControl: false,
  });

  // Dark tiles (CartoDB dark_matter)
  L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    { maxZoom: 19, subdomains: "abcd" }
  ).addTo(map);

  // Zoom control bottom-left
  L.control.zoom({ position: "bottomleft" }).addTo(map);

  // ── Per-drone state ───────────────────────────────────────────────────
  const drones = {};   // { droneId: { marker, trail, trailLine, data } }

  function makeDroneIcon(color) {
    return L.divIcon({
      className: "drone-marker",
      iconSize: [30, 30],
      iconAnchor: [15, 15],
      html: `<div class="ring" style="border:2px solid ${color}"></div>
             <div class="dot" style="background:${color}"></div>`,
    });
  }

  // ── DOM refs ──────────────────────────────────────────────────────────
  const cardsEl     = document.getElementById("drone-cards");
  const connBadge   = document.getElementById("conn-badge");
  const modeBadge   = document.getElementById("mode-badge");
  const clockEl     = document.getElementById("clock");
  const aggRsrp     = document.getElementById("agg-rsrp");
  const aggDl       = document.getElementById("agg-dl");
  const aggLat      = document.getElementById("agg-lat");
  const aggActive   = document.getElementById("agg-active");
  const aggMps      = document.getElementById("agg-mps");
  const resetBtn    = document.getElementById("reset-btn");

  // ── Reset button ─────────────────────────────────────────────────────
  resetBtn.addEventListener("click", () => {
    fetch("/api/reset", { method: "POST" }).catch(err => console.error("Reset failed:", err));
  });

  // ── Message counter ──────────────────────────────────────────────────
  let msgCount = 0;
  let lastCountTs = Date.now();

  // ── Clock ────────────────────────────────────────────────────────────
  function tickClock() {
    const d = new Date();
    clockEl.textContent = d.toLocaleTimeString("en-US", { hour12: false });
  }
  setInterval(tickClock, 1000);
  tickClock();

  // ── Socket.IO ─────────────────────────────────────────────────────────
  const socket = io({ transports: ["websocket", "polling"] });

  socket.on("connect", () => {
    connBadge.textContent = "CONNECTED";
    connBadge.className = "badge badge-on";
  });
  socket.on("disconnect", () => {
    connBadge.textContent = "DISCONNECTED";
    connBadge.className = "badge badge-off";
  });

  socket.on("telemetry", (data) => {
    msgCount++;
    handleTelemetry(data);
  });

  // ── Drone retirement (lifecycle cycling) ──────────────────────────────
  socket.on("drone_retired", (data) => {
    const id = data.drone_id;
    if (drones[id]) {
      // Remove marker and trail from map
      map.removeLayer(drones[id].marker);
      map.removeLayer(drones[id].trailLine);
      delete drones[id];
    }
    // Remove card
    const card = document.getElementById("card-" + id);
    if (card) {
      card.classList.add("card-fade-out");
      setTimeout(() => card.remove(), 600);
    }
    updateAggregates();
  });

  // ── Fleet reset (all drones cleared server-side) ───────────────────────
  socket.on("fleet_reset", () => {
    Object.values(drones).forEach(d => {
      if (d.marker) map.removeLayer(d.marker);
      if (d.trailLine) map.removeLayer(d.trailLine);
    });
    Object.keys(drones).forEach(k => delete drones[k]);
    cardsEl.innerHTML = "";
    updateAggregates();
    clearHeatmap();
  });

  // ── Main handler ──────────────────────────────────────────────────────
  function handleTelemetry(d) {
    const id = d.drone_id;
    const loc = d.location || {};
    const net = d.network || {};
    const lat = loc.latitude;
    const lng = loc.longitude;
    const rsrp = net.signal_rsrp_dbm || -999;
    const color = rsrpColor(rsrp);

    // Create drone entry if new
    if (!drones[id]) {
      // Marker
      const marker = L.marker([lat, lng], { icon: makeDroneIcon(color) })
        .addTo(map)
        .bindTooltip("", { className: "drone-tip", direction: "right", offset: [16, 0] });

      // Trail polyline
      const trailLine = L.polyline([], {
        color: color,
        weight: 2.5,
        opacity: 0.7,
        smoothFactor: 1,
      }).addTo(map);

      drones[id] = { marker, trail: [], trailLine, data: d };
    }

    const drone = drones[id];
    drone.data = d;

    // Update marker position + icon colour
    drone.marker.setLatLng([lat, lng]);
    drone.marker.setIcon(makeDroneIcon(color));
    drone.marker.setTooltipContent(
      `<strong>${id}</strong><br/>` +
      `RSRP ${rsrp} dBm &middot; DL ${net.downlink_mbps || 0} Mbps<br/>` +
      `Bat ${d.battery_pct || 0}% &middot; ${(d.status || "").toUpperCase()}`
    );

    // Trail
    drone.trail.push([lat, lng]);
    if (drone.trail.length > TRAIL_LEN) drone.trail.shift();
    drone.trailLine.setLatLngs(drone.trail);
    drone.trailLine.setStyle({ color });

    // Record signal sample for heatmap grid
    recordHeatSample(lat, lng, rsrp);

    // Update card
    upsertCard(id, d);

    // Aggregates
    updateAggregates();
  }

  // ── Card rendering ───────────────────────────────────────────────────
  function upsertCard(id, d) {
    const net = d.network || {};
    const loc = d.location || {};
    const rsrp = net.signal_rsrp_dbm || 0;
    const bat = d.battery_pct || 0;

    let card = document.getElementById("card-" + id);
    if (!card) {
      card = document.createElement("div");
      card.id = "card-" + id;
      card.className = "drone-card";
      cardsEl.appendChild(card);
    }

    const statusCls = "status-" + (d.status || "patrolling");

    card.innerHTML = `
      <div class="card-header">
        <span class="card-title">${id}</span>
        <span class="card-status ${statusCls}">${(d.status || "—").toUpperCase()}</span>
      </div>
      <div class="metric-grid">
        <div class="metric">
          <span class="metric-label">RSRP</span>
          <span class="metric-value ${rsrpClass(rsrp)}">${rsrp} dBm</span>
        </div>
        <div class="metric">
          <span class="metric-label">SINR</span>
          <span class="metric-value">${(net.signal_sinr_db || 0).toFixed(1)} dB</span>
        </div>
        <div class="metric">
          <span class="metric-label">DL</span>
          <span class="metric-value">${(net.downlink_mbps || 0).toFixed(0)} Mbps</span>
        </div>
        <div class="metric">
          <span class="metric-label">UL</span>
          <span class="metric-value">${(net.uplink_mbps || 0).toFixed(0)} Mbps</span>
        </div>
        <div class="metric">
          <span class="metric-label">Latency</span>
          <span class="metric-value">${(net.latency_ms || 0).toFixed(1)} ms</span>
        </div>
        <div class="metric">
          <span class="metric-label">Loss</span>
          <span class="metric-value">${(net.packet_loss_pct || 0).toFixed(2)}%</span>
        </div>
        <div class="metric">
          <span class="metric-label">Alt</span>
          <span class="metric-value">${(loc.altitude_m || 0).toFixed(0)} m</span>
        </div>
        <div class="metric">
          <span class="metric-label">Speed</span>
          <span class="metric-value">${(loc.speed_mps || 0).toFixed(1)} m/s</span>
        </div>
      </div>
      <div class="battery-row">
        <span class="metric-label" style="min-width:50px">BAT</span>
        <div class="battery-bar-bg">
          <div class="battery-bar-fill" style="width:${bat}%;background:${batteryColor(bat)}"></div>
        </div>
        <span class="battery-label" style="color:${batteryColor(bat)}">${bat.toFixed(0)}%</span>
      </div>`;

    // Flash
    card.classList.add("pulse");
    setTimeout(() => card.classList.remove("pulse"), CARD_PULSE_MS);
  }

  // ── Aggregate stats ──────────────────────────────────────────────────
  function updateAggregates() {
    const ids = Object.keys(drones);
    if (!ids.length) return;

    let sumRsrp = 0, sumDl = 0, sumLat = 0, active = 0;
    ids.forEach((id) => {
      const d = drones[id].data;
      const net = d.network || {};
      sumRsrp += net.signal_rsrp_dbm || 0;
      sumDl   += net.downlink_mbps || 0;
      sumLat  += net.latency_ms || 0;
      const st = (d.status || "").toLowerCase();
      if (st !== "landed" && st !== "charging" && st !== "landing") active++;
    });
    const n = ids.length;
    aggRsrp.textContent   = (sumRsrp / n).toFixed(0) + " dBm";
    aggRsrp.style.color   = rsrpColor(sumRsrp / n);
    aggDl.textContent     = (sumDl / n).toFixed(0) + " Mbps";
    aggLat.textContent    = (sumLat / n).toFixed(1) + " ms";
    aggActive.textContent = active;

    // Messages per second
    const now = Date.now();
    const elapsed = (now - lastCountTs) / 1000;
    if (elapsed >= 2) {
      aggMps.textContent = (msgCount / elapsed).toFixed(1);
      msgCount = 0;
      lastCountTs = now;
    }
  }

  // ── Detect mode (demo vs live) ────────────────────────────────────────
  // Fetch initial state and detect mode
  fetch("/api/state")
    .then((r) => r.json())
    .then((state) => {
      const keys = Object.keys(state);
      if (keys.length) {
        modeBadge.textContent = "LIVE";
      }
      keys.forEach((id) => handleTelemetry(state[id]));
    })
    .catch(() => {});

  // Set mode badge after a short delay (demo mode will have been set by then)
  setTimeout(() => {
    if (modeBadge.textContent === "—") {
      modeBadge.textContent = "DEMO";
    }
  }, 4000);

  // ── Edge AI Insights ─────────────────────────────────────────────────
  const aiBadge       = document.getElementById("ai-badge");
  const aiSummary     = document.getElementById("ai-summary");
  const aiList        = document.getElementById("ai-insights-list");

  const SEVERITY_ICONS = { info: "ℹ", warning: "⚠", critical: "✖" };

  function renderInsight(item) {
    const sev = item.severity || "info";
    const el = document.createElement("div");
    el.className = "ai-insight";
    el.innerHTML = `
      <div class="ai-insight-icon severity-${sev}">${SEVERITY_ICONS[sev] || "ℹ"}</div>
      <div class="ai-insight-body">
        <div class="ai-insight-title">${item.title || item.type || ""}</div>
        <div class="ai-insight-detail">${item.detail || ""}</div>
        ${item.affected ? `<div class="ai-insight-meta">${item.affected}</div>` : ""}
      </div>`;
    return el;
  }

  function handleAiInsights(payload) {
    if (!aiList) return;

    // Fleet status badge
    const status = payload.fleet_status || "nominal";
    if (aiBadge) {
      aiBadge.textContent = "AI " + status.toUpperCase();
      aiBadge.className = "badge badge-ai status-" + status;
    }

    // Summary
    if (aiSummary && payload.summary) {
      aiSummary.textContent = payload.summary;
    }

    // Insight cards
    const insights = payload.insights || [];
    aiList.innerHTML = "";
    insights.forEach((item) => {
      aiList.appendChild(renderInsight(item));
    });

    // Flash the section header
    const hdr = document.getElementById("ai-header");
    if (hdr) {
      hdr.classList.add("pulse");
      setTimeout(() => hdr.classList.remove("pulse"), 800);
    }
  }

  // Real-time via Socket.IO
  socket.on("ai_insights", handleAiInsights);

  // Initial fetch + polling fallback (in case WebSocket drops)
  function fetchAiInsights() {
    fetch("/api/ai-insights")
      .then((r) => r.json())
      .then((data) => {
        if (data && (data.insights || data.summary)) handleAiInsights(data);
      })
      .catch(() => {});
  }
  fetchAiInsights();
  setInterval(fetchAiInsights, 15000);

  // ── Signal Heatmap Grid ─────────────────────────────────────────────
  // Paints map grid cells with rolling-average RSRP as drones fly through.
  const GRID_SIZE = 0.0018;          // ~200 m cell at Denver latitude
  const heatCells = {};              // "latKey,lonKey" → { rect, samples: [rsrp…] }
  const heatLayer = L.layerGroup();
  let heatmapVisible = false;
  const heatBtn = document.getElementById("heatmap-btn");
  const MAX_SAMPLES = 20;            // rolling window per cell

  function gridKey(lat, lon) {
    const gLat = Math.floor(lat / GRID_SIZE) * GRID_SIZE;
    const gLon = Math.floor(lon / GRID_SIZE) * GRID_SIZE;
    return `${gLat.toFixed(5)},${gLon.toFixed(5)}`;
  }

  function heatColor(rsrp) {
    if (rsrp >= -80)  return { fill: "#22c55e", opacity: 0.30 };   // good – green
    if (rsrp >= -100) return { fill: "#f59e0b", opacity: 0.28 };   // ok   – amber
    return { fill: "#ef4444", opacity: 0.32 };                     // poor – red
  }

  function recordHeatSample(lat, lon, rsrp) {
    if (!heatmapVisible || rsrp <= -900) return;          // skip if hidden or no data
    const key = gridKey(lat, lon);
    if (!heatCells[key]) {
      const gLat = Math.floor(lat / GRID_SIZE) * GRID_SIZE;
      const gLon = Math.floor(lon / GRID_SIZE) * GRID_SIZE;
      const bounds = [[gLat, gLon], [gLat + GRID_SIZE, gLon + GRID_SIZE]];
      const { fill, opacity } = heatColor(rsrp);
      const rect = L.rectangle(bounds, {
        color: fill, fillColor: fill, fillOpacity: opacity,
        weight: 0.5, opacity: 0.25, interactive: false,
      });
      heatLayer.addLayer(rect);
      heatCells[key] = { rect, samples: [rsrp] };
    } else {
      const cell = heatCells[key];
      cell.samples.push(rsrp);
      if (cell.samples.length > MAX_SAMPLES) cell.samples.shift();
      const avg = cell.samples.reduce((a, b) => a + b, 0) / cell.samples.length;
      const { fill, opacity } = heatColor(avg);
      cell.rect.setStyle({ color: fill, fillColor: fill, fillOpacity: opacity });
    }
  }

  function clearHeatmap() {
    heatLayer.clearLayers();
    Object.keys(heatCells).forEach(k => delete heatCells[k]);
  }

  heatBtn.addEventListener("click", () => {
    heatmapVisible = !heatmapVisible;
    if (heatmapVisible) {
      heatLayer.addTo(map);
      heatBtn.classList.add("active");
    } else {
      map.removeLayer(heatLayer);
      heatBtn.classList.remove("active");
    }
  });

  // ── Cell Tower Overlay ───────────────────────────────────────────────
  const towerBtn     = document.getElementById("tower-btn");
  const towerOverlay = document.getElementById("tower-overlay");
  const towerClose   = document.getElementById("tower-close");
  const towerIframe  = document.getElementById("tower-iframe");
  const towerLoading = document.getElementById("tower-loading");
  let towerLoaded    = false;

  function openTowerOverlay() {
    towerOverlay.classList.remove("hidden");
    towerOverlay.setAttribute("aria-hidden", "false");
    towerBtn.classList.add("active");
    // Lazy-load the iframe on first open
    if (!towerLoaded) {
      towerLoaded = true;
      towerLoading.classList.remove("hidden");
      towerIframe.src = towerIframe.dataset.src;
      towerIframe.addEventListener("load", () => {
        towerLoading.classList.add("hidden");
      }, { once: true });
      towerIframe.addEventListener("error", () => {
        towerLoading.querySelector("span").textContent = "Failed to load cell tower data.";
      }, { once: true });
    }
  }

  function closeTowerOverlay() {
    towerOverlay.classList.add("hidden");
    towerOverlay.setAttribute("aria-hidden", "true");
    towerBtn.classList.remove("active");
  }

  towerBtn.addEventListener("click", () => {
    towerOverlay.classList.contains("hidden") ? openTowerOverlay() : closeTowerOverlay();
  });
  towerClose.addEventListener("click", closeTowerOverlay);

})();
