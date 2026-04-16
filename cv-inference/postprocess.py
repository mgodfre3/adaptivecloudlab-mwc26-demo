#!/usr/bin/env python3
"""YOLOv8 ONNX output post-processing.

Parses raw model output tensors into bounding-box detections with
Non-Maximum Suppression and coordinate rescaling.
"""

from typing import Dict, List

import cv2
import numpy as np


def postprocess_yolo_output(
    raw_output: list,
    confidence_threshold: float = 0.5,
    nms_iou_threshold: float = 0.45,
    input_size: int = 640,
    orig_width: int = 1920,
    orig_height: int = 1080,
    labels: list | None = None,
) -> List[Dict]:
    """Convert raw YOLOv8 ONNX output into a list of detection dicts.

    YOLOv8 ONNX output shape is typically (1, 4+num_classes, 8400) where
    each of the 8400 candidates has [cx, cy, w, h, class_scores...].

    Args:
        raw_output: List of numpy arrays from ONNX Runtime session.run().
        confidence_threshold: Minimum class score to keep a detection.
        nms_iou_threshold: IoU threshold for Non-Maximum Suppression.
        input_size: Model input dimension (assumes square input).
        orig_width: Original video frame width.
        orig_height: Original video frame height.
        labels: List of class label strings.

    Returns:
        List of detection dictionaries with label, confidence, and bbox info.
    """
    if labels is None:
        labels = ["cellular_antenna"]

    output = np.array(raw_output[0])  # shape: (1, 4+C, N)
    if output.ndim == 3:
        output = output[0]  # (4+C, N)

    # Transpose to (N, 4+C) for easier indexing
    predictions = output.T  # (N, 4+C)

    num_classes = predictions.shape[1] - 4
    if num_classes <= 0:
        return []

    # Extract box coordinates (center-x, center-y, width, height) and class scores
    boxes_cxcywh = predictions[:, :4]
    class_scores = predictions[:, 4:]

    # Best class per candidate
    class_ids = np.argmax(class_scores, axis=1)
    confidences = class_scores[np.arange(len(class_ids)), class_ids]

    # Filter by confidence
    mask = confidences >= confidence_threshold
    if not np.any(mask):
        return []

    boxes_cxcywh = boxes_cxcywh[mask]
    confidences = confidences[mask]
    class_ids = class_ids[mask]

    # Convert centre-format to corner-format (x1, y1, x2, y2) in model-input space
    boxes_xyxy = _cxcywh_to_xyxy(boxes_cxcywh)

    # Apply NMS per class via OpenCV
    indices = _multiclass_nms(boxes_xyxy, confidences, class_ids, nms_iou_threshold)
    if len(indices) == 0:
        return []

    # Scale boxes from model-input space to original frame dimensions
    scale_x = orig_width / input_size
    scale_y = orig_height / input_size

    detections: List[Dict] = []
    for idx in indices:
        x1, y1, x2, y2 = boxes_xyxy[idx]
        x1_orig = max(0, int(round(x1 * scale_x)))
        y1_orig = max(0, int(round(y1 * scale_y)))
        x2_orig = min(orig_width, int(round(x2 * scale_x)))
        y2_orig = min(orig_height, int(round(y2 * scale_y)))

        # Normalised xywh (centre-based, 0-1 range)
        cx_norm = round(((x1_orig + x2_orig) / 2) / orig_width, 4)
        cy_norm = round(((y1_orig + y2_orig) / 2) / orig_height, 4)
        w_norm = round((x2_orig - x1_orig) / orig_width, 4)
        h_norm = round((y2_orig - y1_orig) / orig_height, 4)

        cid = int(class_ids[idx])
        label = labels[cid] if cid < len(labels) else f"class_{cid}"

        detections.append(
            {
                "label": label,
                "confidence": round(float(confidences[idx]), 4),
                "bbox_xyxy": [x1_orig, y1_orig, x2_orig, y2_orig],
                "bbox_xywh_norm": [cx_norm, cy_norm, w_norm, h_norm],
            }
        )

    # Sort by confidence descending
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    return detections


def _cxcywh_to_xyxy(boxes: np.ndarray) -> np.ndarray:
    """Convert (cx, cy, w, h) boxes to (x1, y1, x2, y2)."""
    xyxy = np.empty_like(boxes)
    xyxy[:, 0] = boxes[:, 0] - boxes[:, 2] / 2  # x1
    xyxy[:, 1] = boxes[:, 1] - boxes[:, 3] / 2  # y1
    xyxy[:, 2] = boxes[:, 0] + boxes[:, 2] / 2  # x2
    xyxy[:, 3] = boxes[:, 1] + boxes[:, 3] / 2  # y2
    return xyxy


def _multiclass_nms(
    boxes_xyxy: np.ndarray,
    confidences: np.ndarray,
    class_ids: np.ndarray,
    iou_threshold: float,
) -> List[int]:
    """Apply per-class NMS using OpenCV's DNN module."""
    # Offset boxes by class id to prevent cross-class suppression
    max_coord = boxes_xyxy.max()
    offsets = class_ids.astype(np.float32) * (max_coord + 1)
    shifted_boxes = boxes_xyxy.copy()
    shifted_boxes[:, 0] += offsets
    shifted_boxes[:, 1] += offsets
    shifted_boxes[:, 2] += offsets
    shifted_boxes[:, 3] += offsets

    # OpenCV NMS expects (x, y, w, h) format
    x1 = shifted_boxes[:, 0]
    y1 = shifted_boxes[:, 1]
    w = shifted_boxes[:, 2] - shifted_boxes[:, 0]
    h = shifted_boxes[:, 3] - shifted_boxes[:, 1]
    rects = np.stack([x1, y1, w, h], axis=1).tolist()
    scores = confidences.tolist()

    indices = cv2.dnn.NMSBoxes(rects, scores, score_threshold=0.0, nms_threshold=iou_threshold)

    if isinstance(indices, np.ndarray):
        return indices.flatten().tolist()
    if isinstance(indices, (list, tuple)) and len(indices) > 0:
        # OpenCV < 4.7 returns list of lists
        return [int(i[0]) if isinstance(i, (list, np.ndarray)) else int(i) for i in indices]
    return []
