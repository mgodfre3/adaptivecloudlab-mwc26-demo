# Cell Tower Detection Model Guide

This document describes how to train, export, and deploy the YOLOv8
antenna / tower detection model used by the video-dashboard CV pipeline.

**Strategy:** Fine-tune YOLOv8s on cell tower data, export to ONNX, deploy
to the edge cluster PVC. "Trained in Azure, runs at the edge."

---

## Quick Start — Local Training

The `train_tower_model.py` script handles dataset download, fine-tuning,
and ONNX export end-to-end.

```bash
cd cv-inference

# Install dependencies
pip install ultralytics gdown pyyaml

# Full pipeline (downloads Antenna-Dataset from Google Drive, trains, exports)
python train_tower_model.py

# With a local dataset you already have
python train_tower_model.py --data-dir ./datasets/my-tower-data

# Export an existing .pt model to ONNX only
python train_tower_model.py --export-only runs/detect/cell-tower/weights/best.pt
```

The output is `yolov8s-antenna.onnx` — ready for deployment.

---

## Azure ML GPU Training (Recommended)

For fast training with GPU acceleration (~30 min vs 18+ hours on CPU),
submit the training job to Azure ML.

### Prerequisites

```bash
pip install azure-ai-ml azure-identity
az login
```

### Submit Training Job

```bash
cd cv-inference

# Submit to Azure ML (creates GPU compute, trains, downloads ONNX)
python train_azureml.py

# Custom settings
python train_azureml.py \
    --workspace Demo-AML \
    --compute-size Standard_NC6s_v3 \
    --epochs 30 --batch 16

# Submit without waiting (get model later)
python train_azureml.py --no-wait

# Download model from a completed job
python train_azureml.py --download-from <job-name>
```

### What Happens

1. Connects to Azure ML workspace (`Demo-AML` in `kkambow-rg`)
2. Creates/reuses a GPU compute cluster (`Standard_NC6s_v3`)
3. Creates a training environment (PyTorch + CUDA + ultralytics)
4. Submits `train_tower_model.py` as an Azure ML job
5. Job downloads the Antenna-Dataset, trains YOLOv8s, exports ONNX
6. Downloads the resulting `yolov8s-antenna.onnx` to your machine

Track progress in Azure ML Studio — the URL is printed after submission.

---

## Arc Video Indexer BYOM Integration

The video dashboard automatically pushes YOLO detections to Arc Video
Indexer as **custom insights** (Bring Your Own Model).

### How It Works

```
Upload MP4 → CV Inference (YOLO) → Detections
                                       ↓
Upload MP4 → Video Indexer → Indexed    ↓
                                ← PATCH /insights/customInsights
```

1. Video is uploaded and processed by both YOLO (edge) and VI simultaneously
2. After both complete, YOLO detections are grouped by label with time ranges
3. Detections are patched into the VI index via the `customInsights` API
4. Custom insights appear in the VI portal alongside native VI insights

### What Gets Patched

| Field | Example |
|-------|---------|
| Model Name | `Antenna Detection (YOLOv8s — Edge)` |
| Object Type | `Antenna` |
| Time Range | `00:00:05 — 00:00:12` |
| Confidence | `0.95` |

Overlapping detections within 1 second are automatically merged.

### Configuration

BYOM patching activates automatically when:
- VI is configured (env vars set)
- CV inference produces detections
- VI indexing succeeds

No additional configuration needed.

---

## Model Classes

### Default: 1-class Antenna Model (jafaryi/Antenna-Dataset)

| ID | Class    | Description                              |
|----|----------|------------------------------------------|
| 0  | Antenna  | Cell tower antenna head / panel          |

9,156 images at 640x640, YOLO-format labels, train/valid splits included.
Source: [github.com/jafaryi/Antenna-Dataset](https://github.com/jafaryi/Antenna-Dataset)

### Alternative: 5-class Model (authenciat/cell-tower-classification)

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

### Option A: Antenna-Dataset from GitHub (Recommended — No Account Needed)

The default dataset. 9,156 images, 1 class, auto-downloaded by the training script.

- Source: [github.com/jafaryi/Antenna-Dataset](https://github.com/jafaryi/Antenna-Dataset)
- Download: ~2.6 GB from Google Drive (automated via `gdown`)
- Format: YOLO (images + labels, train/valid splits)
- No API key or account required

```bash
python train_tower_model.py  # downloads automatically
```

### Option B: Custom Frames from Drone Footage

For best demo accuracy, add frames from your actual drone footage:

1. Extract frames: `ffmpeg -i DJI_0008.MP4 -vf "fps=1" frames/%04d.jpg`
2. Upload to [Roboflow](https://roboflow.com) (free tier, browser-based labeling)
3. Label cell tower components (antennas, towers, dishes, equipment)
4. Export as YOLOv8 format
5. Merge with the Antenna-Dataset or use standalone

### Option C: COCO Pre-trained (Zero Training, Demo Only)

Use YOLOv8s pre-trained on COCO 80 classes as-is.  The dashboard
filters out nonsensical aerial detections (bicycles, trains, etc.)
via an allowlist in `cv_inference.py`.

```bash
yolo export model=yolov8s.pt format=onnx simplify=True
```

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

---

## Training Results (Current Production Model)

| Metric | Value |
|--------|-------|
| Base Model | YOLOv8s (11.1M params) |
| Dataset | jafaryi/Antenna-Dataset (9,156 images) |
| Classes | 1 (Antenna) |
| Epochs | 11/15 (early stopped) |
| mAP50 | 0.995 |
| mAP50-95 | 0.905 |
| Precision | 0.99996 |
| Recall | 1.0 |
| ONNX Size | 42.7 MB |
| Output Shape | (1, 5, 8400) |
