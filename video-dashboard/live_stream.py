"""Live video stream with real-time YOLOv8 detection overlay.

Streams a pre-recorded drone video in a loop, running ONNX inference on each
frame and pushing annotated JPEG frames over WebSocket.  This creates the
"live drone camera feed" effect for demo purposes.

The stream runs in a background thread and emits:
  - live_frame:   base64 JPEG with bounding boxes drawn
  - live_metrics: FPS, detection count, inference latency
"""

import base64
import logging
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Stream parameters
STREAM_FPS = 5          # target frames per second for the stream
STREAM_QUALITY = 70     # JPEG quality (0-100)
STREAM_WIDTH = 960      # output frame width (aspect ratio preserved)

# Detection overlay colours (BGR)
BOX_COLOURS = [
    (0, 255, 128),   # green
    (255, 200, 0),   # cyan-ish
    (0, 180, 255),   # orange
    (255, 100, 100), # light blue
    (100, 255, 255), # yellow
]


class LiveStreamProcessor:
    """Manages a live inference stream from a video file."""

    def __init__(self, model_path: str, confidence: float = 0.5):
        self.model_path = model_path
        self.confidence = confidence
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._video_path: str | None = None
        self._socketio = None

        # Metrics
        self.fps = 0.0
        self.frame_count = 0
        self.detection_count = 0
        self.avg_inference_ms = 0.0
        self._inference_times: list[float] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, video_path: str, socketio) -> bool:
        """Start streaming from the given video file."""
        if self._running:
            return False
        if not os.path.isfile(video_path):
            logger.error("Live stream: video not found: %s", video_path)
            return False

        self._video_path = video_path
        self._socketio = socketio
        self._running = True
        self.frame_count = 0
        self.detection_count = 0
        self._inference_times = []

        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()
        logger.info("Live stream started: %s", video_path)
        return True

    def stop(self):
        """Stop the live stream."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.info("Live stream stopped")

    def _stream_loop(self):
        """Main streaming loop — runs in background thread."""
        import cv_inference
        from postprocess import postprocess_yolo_output

        session, input_name = cv_inference._get_onnx_session(self.model_path)
        if session is None:
            logger.error("Live stream: cannot load ONNX model")
            self._running = False
            self._socketio.emit("live_error", {"error": "ONNX model not available"})
            return

        labels = cv_inference.get_model_labels(self.model_path)
        num_classes = cv_inference._model_num_classes
        frame_interval = 1.0 / STREAM_FPS

        while self._running:
            cap = cv2.VideoCapture(self._video_path)
            if not cap.isOpened():
                logger.error("Live stream: cannot open video")
                break

            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            # Skip frames in source to match our target FPS
            src_skip = max(1, int(src_fps / STREAM_FPS))

            frame_idx = 0
            loop_start = time.perf_counter()

            while self._running:
                ret, frame = cap.read()
                if not ret:
                    break  # restart from beginning

                frame_idx += 1
                if frame_idx % src_skip != 0:
                    continue

                t0 = time.perf_counter()

                # Resize for output
                scale = STREAM_WIDTH / orig_w
                out_h = int(orig_h * scale)
                display_frame = cv2.resize(frame, (STREAM_WIDTH, out_h))

                # Run inference
                blob = cv_inference.preprocess_frame(frame)
                raw_output = session.run(None, {input_name: blob})

                detections = postprocess_yolo_output(
                    raw_output,
                    confidence_threshold=self.confidence,
                    input_size=cv_inference.INPUT_SIZE,
                    orig_width=orig_w,
                    orig_height=orig_h,
                    labels=labels,
                )

                # Filter COCO aerial if needed
                if num_classes == 80:
                    detections = [d for d in detections
                                  if d["label"] in cv_inference.COCO_AERIAL_ALLOWLIST]

                inference_ms = (time.perf_counter() - t0) * 1000
                self._inference_times.append(inference_ms)
                if len(self._inference_times) > 30:
                    self._inference_times = self._inference_times[-30:]

                # Draw detections on frame
                self._draw_detections(display_frame, detections, scale)
                self._draw_hud(display_frame, detections, inference_ms)

                # Encode as JPEG
                _, buf = cv2.imencode(".jpg", display_frame,
                                      [cv2.IMWRITE_JPEG_QUALITY, STREAM_QUALITY])
                b64 = base64.b64encode(buf.tobytes()).decode("ascii")

                self.frame_count += 1
                self.detection_count = len(detections)
                self.avg_inference_ms = sum(self._inference_times) / len(self._inference_times)
                elapsed = time.perf_counter() - loop_start
                self.fps = self.frame_count / max(elapsed, 0.001)

                # Emit frame + metrics
                self._socketio.emit("live_frame", {"frame": b64})
                self._socketio.emit("live_metrics", {
                    "fps": round(self.fps, 1),
                    "detections": len(detections),
                    "inference_ms": round(inference_ms, 1),
                    "avg_inference_ms": round(self.avg_inference_ms, 1),
                    "total_frames": self.frame_count,
                    "labels": [d["label"] for d in detections],
                })

                # Pace to target FPS
                elapsed_frame = time.perf_counter() - t0
                sleep_time = frame_interval - elapsed_frame
                if sleep_time > 0:
                    time.sleep(sleep_time)

            cap.release()

        self._running = False
        self._socketio.emit("live_stopped", {})

    def _draw_detections(self, frame: np.ndarray, detections: list[dict],
                         scale: float):
        """Draw bounding boxes and labels on the display frame."""
        for i, det in enumerate(detections):
            bbox = det.get("bbox", {})
            h_frame, w_frame = frame.shape[:2]

            x1 = int(bbox.get("x", 0) * w_frame)
            y1 = int(bbox.get("y", 0) * h_frame)
            x2 = int((bbox.get("x", 0) + bbox.get("w", 0)) * w_frame)
            y2 = int((bbox.get("y", 0) + bbox.get("h", 0)) * h_frame)

            colour = BOX_COLOURS[i % len(BOX_COLOURS)]
            cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)

            # Label with confidence
            label = f"{det['label']} {det['confidence']:.0%}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (x1, y1 - th - 8), (x1 + tw + 6, y1), colour, -1)
            cv2.putText(frame, label, (x1 + 3, y1 - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)

    def _draw_hud(self, frame: np.ndarray, detections: list[dict],
                  inference_ms: float):
        """Draw heads-up display overlay with live metrics."""
        h, w = frame.shape[:2]

        # Semi-transparent background strip at top
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (w, 36), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

        # HUD text
        hud_items = [
            f"LIVE",
            f"FPS: {self.fps:.1f}",
            f"Detections: {len(detections)}",
            f"Inference: {inference_ms:.0f}ms",
        ]
        x_pos = 10
        for item in hud_items:
            colour = (0, 0, 255) if item == "LIVE" else (0, 255, 128)
            cv2.putText(frame, item, (x_pos, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, colour, 1, cv2.LINE_AA)
            (tw, _), _ = cv2.getTextSize(item, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            x_pos += tw + 20

        # Blinking record indicator
        if int(time.time() * 2) % 2:
            cv2.circle(frame, (w - 20, 18), 6, (0, 0, 255), -1)
