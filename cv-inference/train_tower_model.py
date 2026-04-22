#!/usr/bin/env python3
"""Fine-tune YOLOv8 on cell tower / antenna detection data.

This script downloads a cell tower antenna dataset, fine-tunes YOLOv8s,
and exports the resulting model to ONNX for edge deployment in the
video-dashboard pipeline.

Dataset source: https://github.com/jafaryi/Antenna-Dataset
  - 9,156 images (640x640) with YOLO-format bounding box labels
  - 1 class: "Antenna Head"
  - Train/valid splits included

Usage:
    # Full pipeline: download data, train, export
    python train_tower_model.py

    # Train with a local dataset you've already downloaded
    python train_tower_model.py --data-dir ./datasets/antenna_dataset

    # Resume from a previous training run
    python train_tower_model.py --resume runs/detect/train/weights/last.pt

    # Export an existing .pt model to ONNX without retraining
    python train_tower_model.py --export-only runs/detect/train/weights/best.pt

Environment:
    Requires a GPU for reasonable training time (~20-40 min on a single
    NVIDIA GPU).  CPU training works but takes several hours.

    pip install ultralytics gdown

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
import zipfile
from pathlib import Path


GDRIVE_FILE_ID = "1jFjSSOv4nJ_-z-rTVW3mcS-uE5K7S9_p"
DATASET_URL = f"https://drive.google.com/uc?id={GDRIVE_FILE_ID}"


def download_antenna_dataset(dest_dir: str) -> str:
    """Download the Antenna-Dataset from Google Drive (jafaryi/Antenna-Dataset).

    The dataset is ~2.6 GB.  Returns the path to the generated data.yaml.
    """
    try:
        import gdown
    except ImportError:
        print("Installing gdown...")
        os.system(f"{sys.executable} -m pip install gdown --quiet")
        import gdown

    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(dest_dir, "antenna_dataset.zip")

    if not os.path.isfile(zip_path):
        print(f"Downloading Antenna-Dataset (~2.6 GB) to {zip_path}...")
        gdown.download(DATASET_URL, zip_path, quiet=False)
    else:
        print(f"Using cached download: {zip_path}")

    # Extract
    extract_dir = os.path.join(dest_dir, "antenna_dataset")
    if not os.path.isdir(extract_dir):
        print("Extracting dataset...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(dest_dir)
        print(f"Extracted to {dest_dir}")

    # The dataset structure after extraction:
    #   antenna_dataset/train/images/  + labels/
    #   antenna_dataset/valid/images/  + labels/
    # We need to find the actual directories
    data_yaml = _create_data_yaml(dest_dir)
    return data_yaml


def _create_data_yaml(dataset_root: str) -> str:
    """Generate a data.yaml for the antenna dataset.

    Searches for train/images and valid/images directories and writes
    the YAML config that Ultralytics expects.
    """
    import yaml

    train_images = None
    val_images = None

    for root, dirs, files in os.walk(dataset_root):
        rel = os.path.relpath(root, dataset_root).replace("\\", "/")
        # Look for train/images or train/image directories
        if ("train" in rel.lower()) and any(
            d.lower() in ("images", "image") for d in dirs
        ):
            img_dir = next(d for d in dirs if d.lower() in ("images", "image"))
            candidate = os.path.join(root, img_dir)
            # Prefer the larger set (general dataset over sunlight subset)
            if train_images is None or len(os.listdir(candidate)) > len(
                os.listdir(train_images)
            ):
                train_images = candidate

        if ("valid" in rel.lower() or "val" in rel.lower()) and any(
            d.lower() in ("images", "image") for d in dirs
        ):
            img_dir = next(d for d in dirs if d.lower() in ("images", "image"))
            candidate = os.path.join(root, img_dir)
            if val_images is None or len(os.listdir(candidate)) > len(
                os.listdir(val_images)
            ):
                val_images = candidate

    if not train_images:
        print(f"ERROR: Could not find train/images in {dataset_root}")
        print("Directory contents:")
        for root, dirs, files in os.walk(dataset_root):
            level = root.replace(dataset_root, "").count(os.sep)
            if level < 4:
                indent = " " * 2 * level
                print(f"{indent}{os.path.basename(root)}/")
        sys.exit(1)

    # If no validation set found, we'll let ultralytics auto-split
    data = {
        "path": os.path.abspath(dataset_root),
        "train": os.path.relpath(train_images, dataset_root).replace("\\", "/"),
        "nc": 1,
        "names": ["Antenna"],
    }
    if val_images:
        data["val"] = os.path.relpath(val_images, dataset_root).replace("\\", "/")
    else:
        # Use train as val too — ultralytics will handle val split if needed
        data["val"] = data["train"]
        print("WARNING: No validation set found, using train set for validation")

    yaml_path = os.path.join(dataset_root, "data.yaml")
    with open(yaml_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False)

    n_train = len(os.listdir(train_images))
    n_val = len(os.listdir(val_images)) if val_images else 0
    print(f"Dataset: {n_train} train images, {n_val} val images")
    print(f"data.yaml: {yaml_path}")
    return yaml_path


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
        dest = os.path.join("datasets", "antenna-dataset")
        data_yaml = download_antenna_dataset(dest)

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
