"""GPU metrics collector for the drone dashboard.

Queries DCGM exporter (Prometheus text format) on the cluster to surface
real-time GPU utilisation, memory, temperature, and power draw.
"""

import logging
import os
import re
import threading
import time

import httpx

logger = logging.getLogger(__name__)

DCGM_ENDPOINT = os.getenv(
    "DCGM_EXPORTER_URL",
    "http://dcgm-exporter.monitoring.svc.cluster.local:9400/metrics",
)

METRIC_KEYS = {
    "DCGM_FI_DEV_GPU_UTIL":    "gpu_utilization",
    "DCGM_FI_DEV_FB_USED":     "memory_used_mb",
    "DCGM_FI_DEV_FB_FREE":     "memory_free_mb",
    "DCGM_FI_DEV_GPU_TEMP":    "temperature_c",
    "DCGM_FI_DEV_POWER_USAGE": "power_watts",
    "DCGM_FI_DEV_SM_CLOCK":    "sm_clock_mhz",
    "DCGM_FI_DEV_MEM_CLOCK":   "mem_clock_mhz",
}

_METRIC_RE = re.compile(
    r'^(?P<name>[A-Z_]+)\{[^}]*\}\s+(?P<value>[0-9.eE+\-]+)', re.MULTILINE
)

_cache_lock = threading.Lock()
_cached_metrics: dict | None = None
_cache_ts: float = 0
_CACHE_TTL = 2.0


def fetch_gpu_metrics() -> dict:
    """Fetch current GPU metrics from DCGM exporter."""
    global _cached_metrics, _cache_ts

    with _cache_lock:
        if _cached_metrics and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cached_metrics

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(DCGM_ENDPOINT)
            resp.raise_for_status()
            raw = resp.text

        metrics = _parse_dcgm(raw)
        metrics["status"] = "healthy"
        metrics["timestamp"] = time.time()

        used = metrics.get("memory_used_mb", 0)
        free = metrics.get("memory_free_mb", 0)
        total = used + free
        if total > 0:
            metrics["memory_total_mb"] = round(total)
            metrics["memory_percent"] = round(used / total * 100, 1)

        with _cache_lock:
            _cached_metrics = metrics
            _cache_ts = time.time()

        return metrics

    except Exception as exc:
        logger.debug("DCGM fetch failed: %s", exc)
        return {
            "status": "unavailable",
            "gpu_utilization": 0,
            "memory_used_mb": 0,
            "memory_free_mb": 0,
            "memory_total_mb": 0,
            "memory_percent": 0,
            "temperature_c": 0,
            "power_watts": 0,
            "timestamp": time.time(),
        }


def _parse_dcgm(raw_text: str) -> dict:
    result: dict = {}
    for match in _METRIC_RE.finditer(raw_text):
        name = match.group("name")
        if name in METRIC_KEYS:
            try:
                value = float(match.group("value"))
                result[METRIC_KEYS[name]] = round(value, 2)
            except ValueError:
                pass
    return result
