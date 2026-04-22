#!/usr/bin/env python3
"""Fine-tune YOLOv8 on cell tower / antenna detection data.

This script downloads a cell tower dataset from Roboflow, fine-tunes
YOLOv8s, and exports the resulting model to ONNX for edge deployment
in the video-dashboard pipeline.

Usage:
    # Full pipeline: download data, train, export
    python train_tower_model.py

    # Train with a local dataset you've already downloaded
    python train_tower_model.py --data-dir ./datasets/cell-towers

    # Resume from a previous training run
    python train_tower_model.py --resume runs/detect/train/weights/last.pt

    # Export an existing .pt model to ONNX without retraining
    python train_tower_model.py --export-only runs/detect/train/weights/best.pt

Environment:
    Requires a GPU for reasonable training time (~20-40 min on a single
    NVIDIA GPU).  CPU training works but takes several hours.

    pip install ultralytics roboflow

After training:
    1. Copy the ONNX model to the cluster PVC:
       kubectl cp best.onnx video-analysis/<pod>:/data/models/yolov8s-antenna.onnx

    2. Restart the video-dashboard pod to pick up the new model:
       kubectl rollout restart deployment/video-dashboard -n video-analysis

    The dashboard auto-detects the class count from the model and uses the
    correct labels (no code change needed).
"""

import argparse
import os
import shutil
import sys
from pathlib import Path


def download_roboflow_dataset(dest_dir: str, api_key: str | None = None) -> str:
    """Download a cell tower object detection dataset from Roboflow.

    Returns the path to the dataset directory containing data.yaml.
    """
    try:
        from roboflow import Roboflow
    except ImportError:
        print("Installing roboflow...")
        os.system(f"{sys.executable} -m pip install roboflow --quiet")
        from roboflow import Roboflow

    key = api_key or os.environ.get("ROBOFLOW_API_KEY")
    if not key:
        print(
            "\n⚠️  No Roboflow API key found.\n"
            "   Get a free key at https://app.roboflow.com/settings/api\n"
            "   Then set ROBOFLOW_API_KEY env var or pass --roboflow-key.\n"
            "\n   Alternatively, manually download a YOLOv8 dataset and point\n"
            "   --data-dir at the folder containing data.yaml.\n"
        )
        sys.exit(1)

    rf = Roboflow(api_key=key)

    # Primary: RF100 Cellular Towers (object detection, YOLOv8 format)
    # https://universe.roboflow.com/roboflow-j99jq/rf-100-cellular-towers-vkzrq
    print("Downloading RF100 Cellular Towers dataset from Roboflow...")
    project = rf.workspace("roboflow-j99jq").project("rf-100-cellular-towers-vkzrq")
    version = project.version(1)
    dataset = version.download("yolov8", location=dest_dir)
    print(f"Dataset downloaded to {dataset.location}")
    return dataset.location


def find_data_yaml(data_dir: str) -> str:
    """Find data.yaml in a dataset directory."""
    candidates = [
        os.path.join(data_dir, "data.yaml"),
        os.path.join(data_dir, "dataset.yaml"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    # Search recursively
    for root, _, files in os.walk(data_dir):
        for f in files:
            if f in ("data.yaml", "dataset.yaml"):
                return os.path.join(root, f)

    print(f"ERROR: No data.yaml found in {data_dir}")
    sys.exit(1)


def train(
    data_yaml: str,
    base_model: str = "yolov8s.pt",
    epochs: int = 30,
    imgsz: int = 640,
    batch: int = 16,
    resume: str | None = None,
    project: str = "runs/detect",
    name: str = "cell-tower",
) -> str:
    """Fine-tune YOLOv8 and return the path to best.pt."""
    from ultralytics import YOLO

    if resume:
        print(f"Resuming training from {resume}")
        model = YOLO(resume)
        model.train(data=data_yaml, resume=True)
    else:
        print(f"Fine-tuning {base_model} on {data_yaml}")
        print(f"  epochs={epochs}  imgsz={imgsz}  batch={batch}")
        model = YOLO(base_model)
        model.train(
            data=data_yaml,
            epochs=epochs,
            imgsz=imgsz,
            batch=batch,
            project=project,
            name=name,
            exist_ok=True,
            pretrained=True,
            patience=10,
            lr0=0.001,
            lrf=0.01,
            augment=True,
            # Freeze the backbone for the first few epochs to preserve
            # pre-trained features (helpful with small datasets)
            freeze=10,
        )

    best_pt = os.path.join(project, name, "weights", "best.pt")
    if not os.path.isfile(best_pt):
        # Fallback: search for best.pt in runs/
        for root, _, files in os.walk(project):
            if "best.pt" in files:
                best_pt = os.path.join(root, "best.pt")
                break

    print(f"\n✅ Training complete. Best weights: {best_pt}")

    # Print validation metrics
    model = YOLO(best_pt)
    metrics = model.val(data=data_yaml)
    print(f"   mAP50:    {metrics.box.map50:.4f}")
    print(f"   mAP50-95: {metrics.box.map:.4f}")
    print(f"   Classes:  {model.names}")

    return best_pt


def export_onnx(pt_path: str, imgsz: int = 640) -> str:
    """Export a .pt model to ONNX format. Returns the .onnx path."""
    from ultralytics import YOLO

    print(f"Exporting {pt_path} to ONNX...")
    model = YOLO(pt_path)
    onnx_path = model.export(format="onnx", simplify=True, imgsz=imgsz)
    print(f"✅ ONNX exported: {onnx_path}")

    # Also save class names for reference
    names_path = str(Path(onnx_path).with_suffix(".classes.txt"))
    with open(names_path, "w") as f:
        for idx, name in sorted(model.names.items()):
            f.write(f"{idx}: {name}\n")
    print(f"   Class names saved: {names_path}")

    return str(onnx_path)


def main():
    parser = argparse.ArgumentParser(
        description="Fine-tune YOLOv8 for cell tower detection and export ONNX"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Path to an existing dataset with data.yaml (skip download)",
    )
    parser.add_argument(
        "--roboflow-key",
        default=None,
        help="Roboflow API key (or set ROBOFLOW_API_KEY env var)",
    )
    parser.add_argument(
        "--base-model",
        default="yolov8s.pt",
        help="Base YOLO model to fine-tune (default: yolov8s.pt)",
    )
    parser.add_argument("--epochs", type=int, default=30, help="Training epochs")
    parser.add_argument("--batch", type=int, default=16, help="Batch size")
    parser.add_argument("--imgsz", type=int, default=640, help="Image size")
    parser.add_argument(
        "--resume",
        default=None,
        help="Resume training from a checkpoint (path to last.pt)",
    )
    parser.add_argument(
        "--export-only",
        default=None,
        help="Skip training; just export this .pt file to ONNX",
    )
    parser.add_argument(
        "--output",
        default="yolov8s-antenna.onnx",
        help="Final ONNX output filename",
    )

    args = parser.parse_args()

    # ── Export-only mode ──────────────────────────────────────────────
    if args.export_only:
        onnx_path = export_onnx(args.export_only, imgsz=args.imgsz)
        final = args.output
        if onnx_path != final:
            shutil.copy2(onnx_path, final)
            print(f"Copied to {final}")
        return

    # ── Get dataset ───────────────────────────────────────────────────
    if args.data_dir:
        data_yaml = find_data_yaml(args.data_dir)
    else:
        dest = os.path.join("datasets", "cell-towers")
        data_loc = download_roboflow_dataset(dest, api_key=args.roboflow_key)
        data_yaml = find_data_yaml(data_loc)

    print(f"Using dataset config: {data_yaml}")

    # ── Train ─────────────────────────────────────────────────────────
    best_pt = train(
        data_yaml=data_yaml,
        base_model=args.base_model,
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        resume=args.resume,
    )

    # ── Export to ONNX ────────────────────────────────────────────────
    onnx_path = export_onnx(best_pt, imgsz=args.imgsz)

    # Copy to the desired output name
    final = args.output
    if onnx_path != final:
        shutil.copy2(onnx_path, final)
    print(f"\n🚀 Ready for deployment: {final}")
    print(f"   Upload to cluster PVC:")
    print(f"   kubectl cp {final} video-analysis/<pod>:/data/models/yolov8s-antenna.onnx")


if __name__ == "__main__":
    main()
