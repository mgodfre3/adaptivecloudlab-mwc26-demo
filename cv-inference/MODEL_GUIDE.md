# Pre-Trained Model Acquisition Guide
# ====================================
# This document describes how to obtain and prepare the YOLOv8 antenna
# detection model used by the CV inference pipeline.
#
# Strategy: Use existing datasets + minimal fine-tuning in Azure,
# then export ONNX for edge deployment. "Trained in Azure, runs at the edge."

## Quick Start (Demo-Ready in < 2 Hours)

### Option A: Roboflow RF100 Cell Tower Dataset (Fastest)

1. Download from Roboflow Universe:
   https://universe.roboflow.com/roboflow-j99jq/rf-100-cellular-towers-vkzrq

2. Select "YOLOv8" export format → downloads train/val/test splits + data.yaml

3. Fine-tune locally or in Azure ML:
   ```bash
   pip install ultralytics
   yolo train model=yolov8s.pt data=path/to/data.yaml epochs=20 imgsz=640 batch=8
   ```

4. Export to ONNX:
   ```bash
   yolo export model=runs/detect/train/weights/best.pt format=onnx simplify=True
   ```

5. Push to ACR:
   ```bash
   az acr login --name acxcontregwus2
   # Tag and push as OCI artifact, or bake into the cv-inference container image
   ```

### Option B: Azure Custom Vision (Cloud-Managed)

1. Create Custom Vision project at https://customvision.ai
   - Project type: Object Detection
   - Domain: General (compact) for ONNX export

2. Upload and label drone footage frames (50-100 images minimum)
   - Tag: "antenna_panel"

3. Train (Quick Training is fine for demo)

4. Export as ONNX → download model.onnx

5. Place in cv-inference/models/ or push to ACR

### Option C: Pre-Built Demo Model (Zero Training)

For demos where model accuracy is secondary to showing the pipeline:

1. Use YOLOv8s pre-trained on COCO (detects 80 classes including "cell phone",
   "tv", "laptop" — useful for general object detection demo)

2. Download: https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8s.pt

3. Export to ONNX:
   ```bash
   yolo export model=yolov8s.pt format=onnx simplify=True
   ```

4. The inference pipeline will detect general objects — relabel "cell phone"
   detections as "antenna" for demo purposes (configure class mapping in inference.py)

## Recommended: Option A + Custom Frames

Best balance of effort vs. realism:
1. Start with RF100 cell tower dataset (~300 images)
2. Add 50-100 frames from actual drone footage
3. Label with Roboflow (free tier, browser-based)
4. Fine-tune YOLOv8s for 15-20 epochs
5. Export ONNX → deploy

Training time: ~30 min on single GPU (Azure ML NC6s_v3)
Expected mAP: 0.65-0.80 (good enough for demo)

## Model Files Location

After training/export, place model files at:
- `cv-inference/models/yolov8s-antenna.onnx` (primary)
- `cv-inference/models/yolov8s-antenna.pt` (PyTorch backup)
- `cv-inference/models/classes.yaml` (class names)

The Dockerfile copies these into the container image at build time.
