#!/usr/bin/env python3
"""YOLOv8 antenna detection inference pipeline.

Processes drone video through an ONNX-exported YOLOv8 model on GPU,
producing structured detection JSON and optional annotated video output.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from postprocess import postprocess_yolo_output
from annotate import VideoAnnotator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Default class labels for the antenna detection model
CLASS_LABELS = [
    "cellular_antenna",
    "microwave_dish",
    "small_cell",
    "radio_unit",
]

INPUT_SIZE = 640


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run YOLOv8 antenna detection on drone video"
    )
    parser.add_argument(
        "--input", required=True, help="Path to input MP4 video file"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory for output files"
    )
    parser.add_argument(
        "--model",
        default="/models/yolov8s-antenna.onnx",
        help="Path to ONNX model (default: /models/yolov8s-antenna.onnx)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.5,
        help="Detection confidence threshold (default: 0.5)",
    )
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="Produce annotated video with bounding boxes burned in",
    )
    parser.add_argument(
        "--labels",
        nargs="+",
        default=None,
        help="Override class labels (space-separated)",
    )
    return parser.parse_args()


def load_onnx_model(model_path: str) -> ort.InferenceSession:
    """Load ONNX model with GPU acceleration, falling back to CPU."""
    if not os.path.isfile(model_path):
        logger.error("Model file not found: %s", model_path)
        sys.exit(1)

    providers = []
    available = ort.get_available_providers()
    if "CUDAExecutionProvider" in available:
        providers.append(
            (
                "CUDAExecutionProvider",
                {
                    "device_id": 0,
                    "arena_extend_strategy": "kSameAsRequested",
                    "gpu_mem_limit": 12 * 1024 * 1024 * 1024,  # 12 GB cap
                    "cudnn_conv_algo_search": "DEFAULT",
                },
            )
        )
        logger.info("CUDA execution provider enabled")
    else:
        logger.warning(
            "CUDAExecutionProvider not available; falling back to CPU"
        )
    providers.append("CPUExecutionProvider")

    session = ort.InferenceSession(model_path, providers=providers)
    active = session.get_providers()
    logger.info("ONNX Runtime providers: %s", active)
    return session


def preprocess_frame(frame: np.ndarray) -> np.ndarray:
    """Resize and normalise a BGR frame for YOLOv8 inference.

    Returns a float32 NCHW tensor of shape (1, 3, 640, 640).
    """
    resized = cv2.resize(frame, (INPUT_SIZE, INPUT_SIZE))
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    blob = rgb.astype(np.float32) / 255.0
    blob = np.transpose(blob, (2, 0, 1))  # HWC -> CHW
    return np.expand_dims(blob, axis=0)  # add batch dim


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm."""
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:06.3f}"


def run_inference(args: argparse.Namespace) -> None:
    """Main inference pipeline."""
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    labels = args.labels if args.labels else CLASS_LABELS

    # --- Load model ---
    logger.info("Loading model: %s", args.model)
    session = load_onnx_model(args.model)
    input_name = session.get_inputs()[0].name

    # --- Open video ---
    if not os.path.isfile(args.input):
        logger.error("Input video not found: %s", args.input)
        sys.exit(1)

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        logger.error("Failed to open video: %s", args.input)
        sys.exit(1)

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    logger.info(
        "Video: %s — %d frames, %.1f fps, %dx%d",
        args.input, total_frames, fps, width, height,
    )

    # --- Optional annotated video writer ---
    annotator = None
    if args.annotate:
        annotated_path = str(output_dir / "annotated.mp4")
        annotator = VideoAnnotator(annotated_path, fps, width, height)
        logger.info("Annotated video will be written to %s", annotated_path)

    # --- Process frames ---
    all_detections = []
    total_detection_count = 0
    frames_with_detections = 0
    confidence_sum = 0.0
    frame_idx = 0
    start_time = time.perf_counter()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp_sec = frame_idx / fps
        blob = preprocess_frame(frame)
        raw_output = session.run(None, {input_name: blob})
        detections = postprocess_yolo_output(
            raw_output,
            confidence_threshold=args.confidence,
            input_size=INPUT_SIZE,
            orig_width=width,
            orig_height=height,
            labels=labels,
        )

        if detections:
            frames_with_detections += 1
            frame_record = {
                "frame": frame_idx,
                "timestamp_sec": round(timestamp_sec, 3),
                "timestamp_str": format_timestamp(timestamp_sec),
                "objects": detections,
            }
            all_detections.append(frame_record)
            total_detection_count += len(detections)
            confidence_sum += sum(d["confidence"] for d in detections)

        if annotator:
            annotator.write_frame(frame, detections, frame_idx, timestamp_sec)

        frame_idx += 1
        if frame_idx % 500 == 0:
            elapsed = time.perf_counter() - start_time
            logger.info(
                "Processed %d / %d frames (%.1f fps)",
                frame_idx, total_frames, frame_idx / max(elapsed, 1e-9),
            )

    elapsed_total = time.perf_counter() - start_time
    cap.release()
    if annotator:
        annotator.release()

    # --- Build summary ---
    avg_conf = (
        round(confidence_sum / total_detection_count, 4)
        if total_detection_count > 0
        else 0.0
    )
    fps_processed = round(frame_idx / max(elapsed_total, 1e-9), 1)

    summary = {
        "total_detections": total_detection_count,
        "frames_with_detections": frames_with_detections,
        "avg_confidence": avg_conf,
        "processing_time_sec": round(elapsed_total, 1),
        "fps_processed": fps_processed,
    }

    # --- Write detections.json ---
    detections_payload = {
        "video": os.path.basename(args.input),
        "total_frames": frame_idx,
        "fps": fps,
        "detections": all_detections,
        "summary": summary,
    }
    detections_path = output_dir / "detections.json"
    with open(detections_path, "w", encoding="utf-8") as f:
        json.dump(detections_payload, f, indent=2)
    logger.info("Detections written to %s", detections_path)

    # --- Write summary.json ---
    summary_path = output_dir / "summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "video": os.path.basename(args.input),
                "total_frames": frame_idx,
                "fps": fps,
                **summary,
            },
            f,
            indent=2,
        )
    logger.info("Summary written to %s", summary_path)

    logger.info(
        "Done — %d detections across %d frames in %.1fs (%.1f fps)",
        total_detection_count, frames_with_detections,
        elapsed_total, fps_processed,
    )


if __name__ == "__main__":
    run_inference(parse_args())
