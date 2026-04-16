#!/usr/bin/env python3
"""Convert detections to GeoJSON FeatureCollection.

Merges detection results with a GPS sidecar file to produce
georeferenced Point features for each detection.

GPS sidecar CSV format (one row per frame or timestamp):
    frame,lat,lon,alt_m
    0,40.7128,-74.0060,120.5
    1,40.7129,-74.0061,121.0
    ...
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert detections.json to GeoJSON with optional GPS data"
    )
    parser.add_argument(
        "--detections", required=True, help="Path to detections.json"
    )
    parser.add_argument(
        "--gps",
        default=None,
        help="Path to GPS sidecar CSV (columns: frame,lat,lon,alt_m)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output GeoJSON path (default: <output-dir>/detections.geojson)",
    )
    parser.add_argument(
        "--interpolate",
        action="store_true",
        help="Linearly interpolate GPS for frames without exact match",
    )
    return parser.parse_args()


def load_detections(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_gps_sidecar(path: str) -> Dict[int, Dict[str, float]]:
    """Load GPS CSV into a dict keyed by frame number."""
    gps_data: Dict[int, Dict[str, float]] = {}
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"frame", "lat", "lon"}
        if not required.issubset(set(reader.fieldnames or [])):
            logger.error(
                "GPS CSV must have columns: frame, lat, lon (and optionally alt_m). "
                "Found: %s", reader.fieldnames,
            )
            sys.exit(1)

        for row in reader:
            try:
                frame = int(row["frame"])
                gps_data[frame] = {
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                    "alt_m": float(row.get("alt_m", 0)) if row.get("alt_m") else 0.0,
                }
            except (ValueError, KeyError) as exc:
                logger.warning("Skipping malformed GPS row: %s (%s)", row, exc)
    logger.info("Loaded %d GPS records", len(gps_data))
    return gps_data


def interpolate_gps(
    gps_data: Dict[int, Dict[str, float]], frame: int
) -> Optional[Dict[str, float]]:
    """Linearly interpolate GPS coordinates for a given frame."""
    if frame in gps_data:
        return gps_data[frame]

    frames_sorted = sorted(gps_data.keys())
    if not frames_sorted:
        return None

    # Clamp to range
    if frame <= frames_sorted[0]:
        return gps_data[frames_sorted[0]]
    if frame >= frames_sorted[-1]:
        return gps_data[frames_sorted[-1]]

    # Find bounding frames
    lower = max(f for f in frames_sorted if f < frame)
    upper = min(f for f in frames_sorted if f > frame)
    t = (frame - lower) / (upper - lower)

    lo = gps_data[lower]
    hi = gps_data[upper]
    return {
        "lat": round(lo["lat"] + t * (hi["lat"] - lo["lat"]), 7),
        "lon": round(lo["lon"] + t * (hi["lon"] - lo["lon"]), 7),
        "alt_m": round(lo["alt_m"] + t * (hi["alt_m"] - lo["alt_m"]), 2),
    }


def build_geojson(
    detections_data: Dict[str, Any],
    gps_data: Optional[Dict[int, Dict[str, float]]],
    do_interpolate: bool,
) -> Dict[str, Any]:
    """Build a GeoJSON FeatureCollection from detections and GPS data."""
    features: List[Dict[str, Any]] = []

    for frame_record in detections_data.get("detections", []):
        frame = frame_record["frame"]
        timestamp_sec = frame_record.get("timestamp_sec", 0)
        timestamp_str = frame_record.get("timestamp_str", "")

        # Resolve GPS coordinates
        gps: Optional[Dict[str, float]] = None
        if gps_data:
            if do_interpolate:
                gps = interpolate_gps(gps_data, frame)
            else:
                gps = gps_data.get(frame)

        for obj in frame_record.get("objects", []):
            coordinates = (
                [gps["lon"], gps["lat"], gps["alt_m"]] if gps else [0.0, 0.0, 0.0]
            )

            feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": coordinates,
                },
                "properties": {
                    "label": obj["label"],
                    "confidence": obj["confidence"],
                    "frame": frame,
                    "timestamp_sec": timestamp_sec,
                    "timestamp_str": timestamp_str,
                    "bbox_xyxy": obj.get("bbox_xyxy"),
                    "altitude_m": gps["alt_m"] if gps else None,
                    "has_gps": gps is not None,
                },
            }
            features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "video": detections_data.get("video", ""),
            "total_features": len(features),
            "gps_source": "sidecar_csv" if gps_data else "none",
        },
    }


def main() -> None:
    args = parse_args()

    detections_data = load_detections(args.detections)
    logger.info(
        "Loaded detections for %s (%d detection frames)",
        detections_data.get("video", "unknown"),
        len(detections_data.get("detections", [])),
    )

    gps_data = None
    if args.gps:
        gps_data = load_gps_sidecar(args.gps)

    geojson = build_geojson(detections_data, gps_data, args.interpolate)

    output_path = args.output
    if not output_path:
        output_path = str(Path(args.detections).parent / "detections.geojson")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)
    logger.info(
        "GeoJSON written to %s (%d features)", output_path, len(geojson["features"])
    )


if __name__ == "__main__":
    main()
