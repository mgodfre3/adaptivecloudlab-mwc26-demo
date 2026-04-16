#!/usr/bin/env python3
"""Video annotation module for detection visualisation.

Draws bounding boxes, labels, and overlay information onto video frames
and writes an annotated MP4 file.
"""

from typing import Dict, List

import cv2
import numpy as np

# Visual constants
BOX_COLOR = (0, 255, 0)        # Green (BGR)
TEXT_COLOR = (255, 255, 255)    # White
BG_COLOR = (0, 0, 0)           # Black background for text
BOX_THICKNESS = 2
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE_LABEL = 0.5
FONT_SCALE_OVERLAY = 0.6
FONT_THICKNESS = 1


class VideoAnnotator:
    """Writes annotated video frames with detection overlays."""

    def __init__(
        self, output_path: str, fps: float, width: int, height: int
    ) -> None:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        if not self._writer.isOpened():
            raise RuntimeError(f"Failed to open VideoWriter for {output_path}")
        self._width = width
        self._height = height

    def write_frame(
        self,
        frame: np.ndarray,
        detections: List[Dict],
        frame_idx: int,
        timestamp_sec: float,
    ) -> None:
        """Annotate a single frame and write it to the output video.

        Args:
            frame: Original BGR frame (will be copied, not modified in-place).
            detections: List of detection dicts from postprocess module.
            frame_idx: Current frame number.
            timestamp_sec: Current timestamp in seconds.
        """
        annotated = frame.copy()

        # Draw each detection
        for det in detections:
            x1, y1, x2, y2 = det["bbox_xyxy"]
            label = det["label"]
            conf = det["confidence"]

            # Bounding box
            cv2.rectangle(annotated, (x1, y1), (x2, y2), BOX_COLOR, BOX_THICKNESS)

            # Label with confidence
            text = f"{label} {conf:.2f}"
            (tw, th), baseline = cv2.getTextSize(text, FONT, FONT_SCALE_LABEL, FONT_THICKNESS)
            label_y = max(y1 - 6, th + 4)
            # Background rectangle behind label
            cv2.rectangle(
                annotated,
                (x1, label_y - th - 4),
                (x1 + tw + 4, label_y + baseline),
                BG_COLOR,
                cv2.FILLED,
            )
            cv2.putText(
                annotated, text, (x1 + 2, label_y - 2),
                FONT, FONT_SCALE_LABEL, BOX_COLOR, FONT_THICKNESS, cv2.LINE_AA,
            )

        # Timestamp overlay (top-left)
        ts_text = _format_overlay_time(timestamp_sec)
        _draw_text_with_bg(annotated, ts_text, (10, 30))

        # Frame counter overlay
        frame_text = f"Frame: {frame_idx}"
        _draw_text_with_bg(annotated, frame_text, (10, 60))

        # Detection count overlay
        count_text = f"Detections: {len(detections)}"
        _draw_text_with_bg(annotated, count_text, (10, 90))

        self._writer.write(annotated)

    def release(self) -> None:
        """Flush and close the video writer."""
        self._writer.release()


def _format_overlay_time(seconds: float) -> str:
    hrs = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hrs:02d}:{mins:02d}:{secs:05.2f}"


def _draw_text_with_bg(
    frame: np.ndarray, text: str, origin: tuple
) -> None:
    """Draw text with a semi-transparent dark background."""
    (tw, th), baseline = cv2.getTextSize(text, FONT, FONT_SCALE_OVERLAY, FONT_THICKNESS)
    x, y = origin
    # Dark background
    overlay = frame.copy()
    cv2.rectangle(overlay, (x - 2, y - th - 4), (x + tw + 4, y + baseline + 2), BG_COLOR, cv2.FILLED)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)
    # Text
    cv2.putText(frame, text, (x, y), FONT, FONT_SCALE_OVERLAY, TEXT_COLOR, FONT_THICKNESS, cv2.LINE_AA)
