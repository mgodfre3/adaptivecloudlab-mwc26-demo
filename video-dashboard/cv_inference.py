"""Real YOLOv8 ONNX inference for the video dashboard.

Runs object detection on uploaded drone videos using a pre-trained ONNX model.
Falls back gracefully when the model file is not available (marks step as
unavailable rather than generating synthetic data).

The dashboard container runs on CPU; frame-skipping keeps processing time
reasonable (~2-5 minutes for a typical 2-minute drone video).
"""

import logging
import os
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Lazy-loaded ONNX session (cached after first use)
_onnx_session = None
_onnx_input_name: str | None = None
_model_num_classes: int = 0

INPUT_SIZE = 640

# Default class labels matching the antenna detection model
# These are updated when a trained model is deployed; cv_inference
# auto-detects class count from the ONNX output shape, so these labels
# just need to match the order used during training.
#
# 1-class model (jafaryi/Antenna-Dataset):
ANTENNA_LABELS_1 = ["Antenna"]
# 5-class model (authenciat/cell-tower-classification):
ANTENNA_LABELS_5 = [
    "GSM Antenna",
    "Microwave Antenna",
    "antenna",
    "Lattice Tower",
    "M Type Tower",
]

# COCO 80-class labels (used when the model has 80 output classes)
COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic_light", "fire_hydrant", "stop_sign",
    "parking_meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports_ball", "kite",
    "baseball_bat", "baseball_glove", "skateboard", "surfboard",
    "tennis_racket", "bottle", "wine_glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot_dog", "pizza", "donut", "cake", "chair", "couch", "potted_plant",
    "bed", "dining_table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell_phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy_bear",
    "hair_drier", "toothbrush",
]

# COCO classes plausible in aerial drone footage of cell towers.
# Everything else (bicycle, train, pizza, etc.) is suppressed.
COCO_AERIAL_ALLOWLIST = {
    "person", "car", "truck", "bus", "airplane",
    "bird", "kite",
    "backpack", "umbrella",
    "cell_phone",
    "clock",
    "stop_sign", "fire_hydrant", "traffic_light",
    "chair", "bench",
    "potted_plant",
}

# Frame skip: process every Nth frame to keep CPU runtime manageable
DEFAULT_FRAME_SKIP = 15


def _get_onnx_session(model_path: str):
    """Load (or return cached) ONNX inference session."""
    global _onnx_session, _onnx_input_name, _model_num_classes

    if _onnx_session is not None:
        return _onnx_session, _onnx_input_name

    try:
        import onnxruntime as ort
    except ImportError:
        logger.error("onnxruntime not installed — CV inference unavailable")
        return None, None

    if not os.path.isfile(model_path):
        logger.warning("ONNX model not found at %s — CV inference unavailable", model_path)
        return None, None

    providers = []
    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        providers.append(("CUDAExecutionProvider", {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": 12 * 1024 * 1024 * 1024,
        }))
        logger.info("CV Inference: CUDA execution provider enabled")
    else:
        logger.info("CV Inference: using CPU execution provider")
    providers.append("CPUExecutionProvider")

    _onnx_session = ort.InferenceSession(model_path, providers=providers)
    _onnx_input_name = _onnx_session.get_inputs()[0].name
    active = _onnx_session.get_providers()

    # Detect number of classes from output shape: (1, 4+C, N)
    out_shape = _onnx_session.get_outputs()[0].shape
    if out_shape and len(out_shape) == 3 and isinstance(out_shape[1], int):
        _model_num_classes = out_shape[1] - 4
    else:
        _model_num_classes = 0
    logger.info("CV Inference: ONNX Runtime providers: %s, classes: %d", active, _model_num_classes)
    return _onnx_session, _onnx_input_name


def _parse_onnx_names(metadata_str: str) -> list[str] | None:
    """Safely parse the 'names' field from ONNX model metadata.

    The field is typically a dict-like string: {0: 'Antenna', 1: 'Tower'}.
    Returns an ordered list of class names, or None on failure.
    """
    if not metadata_str:
        return None
    try:
        import ast
        parsed = ast.literal_eval(metadata_str)
        if isinstance(parsed, dict):
            max_idx = max(parsed.keys())
            return [str(parsed.get(i, f"class_{i}")) for i in range(max_idx + 1)]
        if isinstance(parsed, (list, tuple)):
            return [str(n) for n in parsed]
    except Exception:
        pass
    return None


def get_model_labels(model_path: str) -> list[str]:
    """Return the appropriate label list for the loaded model.

    Reads class names from ONNX model metadata first, then falls back
    to hardcoded lists matched by class count.
    """
    session, _ = _get_onnx_session(model_path)  # ensure model is loaded

    # Try reading labels from ONNX model metadata (most reliable)
    if session is not None:
        try:
            meta = session.get_modelmeta()
            names_str = meta.custom_metadata_map.get("names", "")
            parsed = _parse_onnx_names(names_str)
            if parsed and len(parsed) == _model_num_classes:
                logger.info("CV Inference: using labels from ONNX metadata: %s", parsed)
                return parsed
        except Exception as exc:
            logger.debug("Could not read ONNX metadata labels: %s", exc)

    # Fallback: match by class count
    if _model_num_classes == 80:
        return COCO_LABELS
    if _model_num_classes == len(ANTENNA_LABELS_1):
        return ANTENNA_LABELS_1
    if _model_num_classes == len(ANTENNA_LABELS_5):
        return ANTENNA_LABELS_5
    # Last resort: generate generic labels
    return [f"class_{i}" for i in range(_model_num_classes)] if _model_num_classes > 0 else ANTENNA_LABELS_1


def is_model_available(model_path: str) -> bool:
    """Check if the ONNX model file exists."""
    return os.path.isfile(model_path)


def preprocess_frame(frame: np.ndarray) -> np.ndarray:
    """Resize and normalise a BGR frame for YOLOv8 inference.

    Returns a float32 NCHW tensor of shape (1, 3, 640, 640).
    """
    resized = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    blob = rgb.astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))  # HWC -> CHW
    return np.expand_dims(blob, axis=0)


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:06.3f}"


def _enrich_detections(detections: list[dict]) -> None:
    """Add visual descriptor attributes to each detection in-place.

    Adds:
      - size_band: relative size descriptor (small / medium / large)
      - vertical_band: position in frame (upper / mid / lower)
      - confidence_band: detection clarity (high / moderate / low)
      - display_label: human-friendly label combining class + descriptors
    """
    for det in detections:
        bbox = det.get("bbox", {})
        w = bbox.get("w", 0)
        h = bbox.get("h", 0)
        y = bbox.get("y", 0.5)
        area = w * h
        conf = det.get("confidence", 0)
        label = det.get("label", "Object")

        # Size band based on normalised bbox area
        if area > 0.08:
            size_band = "large"
        elif area > 0.02:
            size_band = "medium"
        else:
            size_band = "small"

        # Vertical position in frame (0=top, 1=bottom)
        cy = y + h / 2
        if cy < 0.33:
            vertical_band = "upper"
        elif cy < 0.66:
            vertical_band = "mid"
        else:
            vertical_band = "lower"

        # Confidence band
        if conf >= 0.8:
            confidence_band = "high"
        elif conf >= 0.6:
            confidence_band = "moderate"
        else:
            confidence_band = "low"

        det["size_band"] = size_band
        det["vertical_band"] = vertical_band
        det["confidence_band"] = confidence_band
        det["display_label"] = (
            f"{label} ({size_band} detection, {vertical_band} frame, "
            f"{confidence_band} confidence)"
        )


def run_inference(
    video_path: str,
    model_path: str,
    confidence_threshold: float = 0.5,
    frame_skip: int = DEFAULT_FRAME_SKIP,
    labels: list[str] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[dict], dict]:
    """Run YOLOv8 ONNX inference on a video file.

    Args:
        video_path: Path to the MP4 file.
        model_path: Path to the ONNX model.
        confidence_threshold: Minimum detection confidence.
        frame_skip: Process every Nth frame (1 = every frame).
        labels: Class label strings (auto-detected from model if None).
        progress_callback: Called with (frames_processed, total_frames).

    Returns:
        (detections, summary) where detections is a list of detection dicts
        in the dashboard format, and summary is inference statistics.

    Raises:
        FileNotFoundError: If model or video file doesn't exist.
        RuntimeError: If ONNX session cannot be created.
    """
    from postprocess import postprocess_yolo_output

    if labels is None:
        labels = get_model_labels(model_path)

    session, input_name = _get_onnx_session(model_path)
    if session is None:
        raise RuntimeError(f"Cannot load ONNX model from {model_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = total_frames / fps if fps > 0 else 0

    logger.info(
        "CV Inference: %s — %d frames, %.1f fps, %dx%d, skip=%d",
        video_path, total_frames, fps, width, height, frame_skip,
    )

    all_detections: list[dict] = []
    total_detection_count = 0
    frames_with_detections = 0
    confidence_sum = 0.0
    frames_processed = 0
    frame_idx = 0
    det_counter = 0
    start_time = time.perf_counter()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % frame_skip != 0:
            frame_idx += 1
            continue

        timestamp_sec = frame_idx / fps
        blob = preprocess_frame(frame)
        raw_output = session.run(None, {input_name: blob})

        detections = postprocess_yolo_output(
            raw_output,
            confidence_threshold=confidence_threshold,
            input_size=INPUT_SIZE,
            orig_width=width,
            orig_height=height,
            labels=labels,
        )

        # Filter out nonsensical COCO detections for aerial footage
        if _model_num_classes == 80:
            detections = [d for d in detections if d["label"] in COCO_AERIAL_ALLOWLIST]

        if detections:
            frames_with_detections += 1
            for det in detections:
                # Map cv-inference format → dashboard format
                norm = det.get("bbox_xywh_norm", [0, 0, 0, 0])
                cx_n, cy_n, w_n, h_n = norm[0], norm[1], norm[2], norm[3]
                all_detections.append({
                    "id": f"det-{det_counter:04d}",
                    "frame": frame_idx,
                    "timestamp": round(timestamp_sec, 2),
                    "timestamp_str": format_timestamp(timestamp_sec),
                    "label": det["label"],
                    "confidence": det["confidence"],
                    "is_anomaly": False,  # real model detects components, not defects
                    "bbox": {
                        "x": round(max(0, cx_n - w_n / 2), 4),
                        "y": round(max(0, cy_n - h_n / 2), 4),
                        "w": round(w_n, 4),
                        "h": round(h_n, 4),
                    },
                    "bbox_xyxy": det.get("bbox_xyxy", []),
                    "geo": {},
                })
                det_counter += 1
                total_detection_count += 1
                confidence_sum += det["confidence"]

        frames_processed += 1
        frame_idx += 1

        if progress_callback and frames_processed % 10 == 0:
            progress_callback(frame_idx, total_frames)

    cap.release()
    elapsed = time.perf_counter() - start_time

    # Enrich detections with visual descriptors
    _enrich_detections(all_detections)

    avg_conf = (
        round(confidence_sum / total_detection_count, 4)
        if total_detection_count > 0
        else 0.0
    )

    summary = {
        "total_detections": total_detection_count,
        "frames_processed": frames_processed,
        "frames_with_detections": frames_with_detections,
        "total_frames": total_frames,
        "fps": fps,
        "duration": round(duration, 1),
        "avg_confidence": avg_conf,
        "processing_time_sec": round(elapsed, 1),
        "frame_skip": frame_skip,
        "confidence_threshold": confidence_threshold,
        "source": "onnx-yolov8",
    }

    # Add enrichment aggregates to summary for AI analysis
    summary["enrichment"] = _aggregate_enrichment(all_detections)

    # Sort detections by frame order
    all_detections.sort(key=lambda d: d["frame"])

    logger.info(
        "CV Inference complete: %d detections across %d frames in %.1fs",
        total_detection_count, frames_with_detections, elapsed,
    )

    return all_detections, summary


def _aggregate_enrichment(detections: list[dict]) -> dict:
    """Build aggregated enrichment stats for AI summary consumption."""
    if not detections:
        return {}

    from collections import Counter
    size_counts = Counter(d.get("size_band", "unknown") for d in detections)
    vert_counts = Counter(d.get("vertical_band", "unknown") for d in detections)
    conf_counts = Counter(d.get("confidence_band", "unknown") for d in detections)
    label_counts = Counter(d.get("label", "unknown") for d in detections)

    # Temporal clustering: find continuous detection segments
    timestamps = sorted(set(d["timestamp"] for d in detections))
    segments = []
    if timestamps:
        seg_start = timestamps[0]
        seg_end = timestamps[0]
        for t in timestamps[1:]:
            if t - seg_end <= 2.0:  # within 2 seconds = same segment
                seg_end = t
            else:
                segments.append((round(seg_start, 1), round(seg_end, 1)))
                seg_start = t
                seg_end = t
        segments.append((round(seg_start, 1), round(seg_end, 1)))

    return {
        "by_class": dict(label_counts),
        "by_size": dict(size_counts),
        "by_position": dict(vert_counts),
        "by_confidence": dict(conf_counts),
        "detection_segments": segments,
        "segment_count": len(segments),
    }
