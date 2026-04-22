# Cell Tower Detection Model Guide

This document describes how to train, export, and deploy the YOLOv8
antenna / tower detection model used by the video-dashboard CV pipeline.

**Strategy:** Fine-tune YOLOv8s on cell tower data, export to ONNX, deploy
to the edge cluster PVC. "Trained in Azure, runs at the edge."

---

## Quick Start — Automated Training Script

The `train_tower_model.py` script handles dataset download, fine-tuning,
and ONNX export end-to-end.

```bash
cd cv-inference

# Install dependencies
pip install ultralytics roboflow

# Full pipeline (downloads RF100 cell tower dataset, trains, exports)
export ROBOFLOW_API_KEY=<your-key>
python train_tower_model.py

# With a local dataset you already have
python train_tower_model.py --data-dir ./datasets/my-tower-data

# Export an existing .pt model to ONNX only
python train_tower_model.py --export-only runs/detect/cell-tower/weights/best.pt
```

The output is `yolov8s-antenna.onnx` — ready for deployment.

---

## Model Classes

The model detects 5 classes (aligned with
[authenciat/cell-tower-classification](https://github.com/authenciat/cell-tower-classification)):

| ID | Class              | Description                          |
|----|--------------------|--------------------------------------|
| 0  | GSM Antenna        | Cellular panel / sector antenna      |
| 1  | Microwave Antenna  | Point-to-point microwave dish        |
| 2  | antenna            | Generic antenna (unclassified type)  |
| 3  | Lattice Tower      | Self-supporting lattice tower        |
| 4  | M Type Tower       | Monopole or guyed-mast tower         |

The dashboard (`cv_inference.py`) auto-detects the class count from the
ONNX model output shape and selects the appropriate labels.

---

## Dataset Options

### Option A: Roboflow RF100 Cell Towers (Recommended)

1. Get a free API key at https://app.roboflow.com/settings/api
2. The training script downloads automatically:
   - [RF100 Cellular Towers](https://universe.roboflow.com/roboflow-j99jq/rf-100-cellular-towers-vkzrq)
   - YOLOv8 format with train/val/test splits + `data.yaml`
3. ~300 labeled images, 5+ tower/antenna classes

### Option B: Custom Frames from Drone Footage

For best demo accuracy, add frames from your actual drone footage:

1. Extract frames: `ffmpeg -i DJI_0008.MP4 -vf "fps=1" frames/%04d.jpg`
2. Upload to [Roboflow](https://roboflow.com) (free tier, browser-based labeling)
3. Label cell tower components (antennas, towers, dishes, equipment)
4. Export as YOLOv8 format
5. Merge with the RF100 dataset or use standalone

### Option C: Combined (Best Results)

1. Start with RF100 cell tower dataset (~300 images)
2. Add 50-100 frames from actual drone footage
3. Label in Roboflow
4. Train with merged data:
   ```bash
   python train_tower_model.py --data-dir ./datasets/merged
   ```

Training time: ~30 min on single GPU.  Expected mAP: 0.65-0.80.

### Option D: COCO Pre-trained (Zero Training, Demo Only)

Use YOLOv8s pre-trained on COCO 80 classes as-is.  The dashboard
filters out nonsensical aerial detections (bicycles, trains, etc.)
via an allowlist in `cv_inference.py`.

```bash
yolo export model=yolov8s.pt format=onnx simplify=True
```

This is the current interim model.

---

## Deployment

After training/export:

```bash
# 1. Find the dashboard pod
POD=$(kubectl get pods -n video-analysis -l app=video-dashboard -o name | head -1)

# 2. Upload the ONNX model to the PVC
kubectl cp yolov8s-antenna.onnx video-analysis/${POD#pod/}:/data/models/yolov8s-antenna.onnx

# 3. Restart to pick up the new model
kubectl rollout restart deployment/video-dashboard -n video-analysis
```

The dashboard loads the model from `CV_MODEL_PATH` (default:
`/models/yolov8s-antenna.onnx`).  No code change is needed — the class
count is auto-detected from the ONNX output shape.

---

## COCO Aerial Allowlist

When running the interim COCO model, `cv_inference.py` filters detections
through `COCO_AERIAL_ALLOWLIST` — a set of classes that are plausible in
aerial drone footage (person, car, truck, bird, etc.).  Nonsensical
classes like `bicycle`, `train`, `pizza` are suppressed.

This filter is automatically disabled when a custom-trained model is
detected (class count ≠ 80).

---

## Model Files

| File | Location | Description |
|------|----------|-------------|
| `yolov8s-antenna.onnx` | PVC `/data/models/` | Production ONNX model |
| `best.pt` | `cv-inference/runs/detect/cell-tower/weights/` | PyTorch checkpoint |
| `best.classes.txt` | Next to ONNX file | Class index → name mapping |
