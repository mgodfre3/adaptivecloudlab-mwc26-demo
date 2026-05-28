#!/usr/bin/env python3
"""Train a custom object detection model using Azure Custom Vision.

Downloads the Antenna-Dataset, reads YOLO annotations, uploads images with
bounding box regions to Azure Custom Vision, and starts model training.

Usage:
    python train_azure_vision.py

    # With an already-downloaded dataset
    python train_azure_vision.py --data-dir ./datasets/antenna-dataset

    # Check training status
    python train_azure_vision.py --status

Prerequisites:
    pip install gdown Pillow
    az login
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────
TRAINING_ENDPOINT = "https://acx-cv-training.cognitiveservices.azure.com"
TRAINING_RESOURCE = "acx-cv-training"
PREDICTION_RESOURCE = "acx-cv-prediction"
RESOURCE_GROUP = "ACX-Foundry"
PROJECT_NAME = "antenna-detection"
MAX_IMAGES = 5000  # F0 tier limit

GDRIVE_FILE_ID = "1jFjSSOv4nJ_-z-rTVW3mcS-uE5K7S9_p"


def _az(*args) -> str:
    """Run an Azure CLI command and return stdout. Uses shell=True for Windows."""
    cmd = "az " + " ".join(args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True, shell=True)
    return result.stdout.strip()


def get_training_key() -> str:
    """Get the Custom Vision Training API key via Azure CLI."""
    return _az(
        "cognitiveservices", "account", "keys", "list",
        "--name", TRAINING_RESOURCE,
        "--resource-group", RESOURCE_GROUP,
        "--query", "key1", "-o", "tsv",
    )


# ── Dataset Download ──────────────────────────────────────────────────

def download_dataset(dest_dir: str) -> str:
    """Download and extract the Antenna-Dataset. Returns path to dataset root."""
    try:
        import gdown
    except ImportError:
        os.system(f"{sys.executable} -m pip install gdown --quiet")
        import gdown

    os.makedirs(dest_dir, exist_ok=True)
    archive_path = os.path.join(dest_dir, "antenna_dataset.rar")

    if not os.path.isfile(archive_path):
        url = f"https://drive.google.com/uc?id={GDRIVE_FILE_ID}"
        print(f"Downloading Antenna-Dataset (~2.6 GB)...")
        gdown.download(url, archive_path, quiet=False)
    else:
        print(f"Using cached download: {archive_path}")

    # Extract
    extract_marker = os.path.join(dest_dir, ".extracted")
    if not os.path.isfile(extract_marker):
        print("Extracting dataset (RAR archive)...")
        _extract_rar(archive_path, dest_dir)
        Path(extract_marker).touch()
        print("Extraction complete.")

    return dest_dir


def _extract_rar(rar_path: str, dest_dir: str):
    """Extract RAR archive."""
    for cmd in [
        f'"C:\\Program Files\\7-Zip\\7z.exe" x "{rar_path}" "-o{dest_dir}" -y',
        f'7z x "{rar_path}" "-o{dest_dir}" -y',
        f'unrar x -o+ "{rar_path}" "{dest_dir}{os.sep}"',
    ]:
        try:
            subprocess.run(cmd, check=True, capture_output=True, shell=True)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    # Try Python rarfile
    os.system(f"{sys.executable} -m pip install rarfile --quiet")
    import rarfile
    with rarfile.RarFile(rar_path) as rf:
        rf.extractall(dest_dir)


# ── YOLO Annotation Reader ────────────────────────────────────────────

def find_dataset_splits(dataset_root: str) -> dict:
    """Find train/valid image and label directories."""
    splits = {}
    for root, dirs, _ in os.walk(dataset_root):
        rel = os.path.relpath(root, dataset_root).replace("\\", "/").lower()
        for split_name in ("train", "valid", "val"):
            if split_name in rel:
                img_dirs = [d for d in dirs if d.lower() in ("images", "image")]
                lbl_dirs = [d for d in dirs if d.lower() in ("labels", "label")]
                if img_dirs:
                    key = "train" if "train" in rel else "val"
                    candidate_img = os.path.join(root, img_dirs[0])
                    n = len(os.listdir(candidate_img))
                    if key not in splits or n > splits[key].get("count", 0):
                        splits[key] = {
                            "images": candidate_img,
                            "labels": os.path.join(root, lbl_dirs[0]) if lbl_dirs else None,
                            "count": n,
                        }
    return splits


def get_image_size(img_path: str) -> tuple:
    """Get image dimensions without loading full image."""
    try:
        from PIL import Image
        with Image.open(img_path) as im:
            return im.size  # (width, height)
    except Exception:
        return (640, 640)


def load_yolo_annotations(dataset_root: str, max_images: int = 0):
    """Load YOLO annotations and return list of (image_path, regions) tuples.

    Each region is a dict with normalized coordinates: left, top, width, height (0-1).
    """
    splits = find_dataset_splits(dataset_root)
    if not splits:
        print(f"ERROR: No train/valid splits found in {dataset_root}")
        sys.exit(1)

    summary = ", ".join(f"{k}: {v['count']} images" for k, v in splits.items())
    print(f"Found splits: {summary}")

    all_entries = []

    for split_name, split_info in splits.items():
        img_dir = split_info["images"]
        lbl_dir = split_info["labels"]
        if not lbl_dir or not os.path.isdir(lbl_dir):
            lbl_dir = img_dir.replace("images", "labels").replace("image", "label")

        img_files = sorted([
            f for f in os.listdir(img_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp"))
        ])

        for img_file in img_files:
            img_path = os.path.join(img_dir, img_file)

            # Parse YOLO label file for normalized regions
            label_file = os.path.splitext(img_file)[0] + ".txt"
            label_path = os.path.join(lbl_dir, label_file)
            regions = []

            if os.path.isfile(label_path):
                with open(label_path) as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            cx, cy, bw, bh = map(float, parts[1:5])
                            # Custom Vision uses normalized (left, top, width, height)
                            left = max(0, cx - bw / 2)
                            top = max(0, cy - bh / 2)
                            regions.append({
                                "left": round(left, 6),
                                "top": round(top, 6),
                                "width": round(min(bw, 1.0 - left), 6),
                                "height": round(min(bh, 1.0 - top), 6),
                            })

            if regions:  # Only include images that have annotations
                all_entries.append((img_path, regions))

    if max_images > 0 and len(all_entries) > max_images:
        all_entries = all_entries[:max_images]

    print(f"  Total: {len(all_entries)} annotated images (capped at {max_images or 'all'})")
    return all_entries


# ── Custom Vision API ─────────────────────────────────────────────────

def cv_api(method: str, path: str, api_key: str, body=None,
           content_type="application/json", raw_data=None) -> dict:
    """Call Custom Vision Training REST API."""
    url = f"{TRAINING_ENDPOINT}/customvision/v3.3/training/{path}"
    headers = {
        "Training-Key": api_key,
    }
    if content_type:
        headers["Content-Type"] = content_type

    if raw_data is not None:
        data = raw_data
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_body = resp.read().decode()
            if resp_body:
                return json.loads(resp_body)
            return {"status": resp.status}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(f"  API Error {e.code}: {err_body[:500]}")
        raise


def get_or_create_project(api_key: str) -> str:
    """Get existing project or create a new one. Returns project ID."""
    # List existing projects
    projects = cv_api("GET", "projects", api_key)
    for p in projects:
        if p["name"] == PROJECT_NAME:
            print(f"  Using existing project: {p['name']} ({p['id']})")
            return p["id"]

    # Get ObjectDetection domain ID
    domains = cv_api("GET", "domains", api_key)
    od_domain = None
    for d in domains:
        if d["type"] == "ObjectDetection" and not d.get("exportable", False):
            od_domain = d["id"]
            break

    if not od_domain:
        # Use first ObjectDetection domain
        for d in domains:
            if d["type"] == "ObjectDetection":
                od_domain = d["id"]
                break

    project = cv_api("POST", f"projects?name={PROJECT_NAME}&domainId={od_domain}", api_key)
    print(f"  Created project: {project['name']} ({project['id']})")
    return project["id"]


def get_or_create_tag(api_key: str, project_id: str, tag_name: str) -> str:
    """Get existing tag or create a new one. Returns tag ID."""
    tags = cv_api("GET", f"projects/{project_id}/tags", api_key)
    for t in tags:
        if t["name"] == tag_name:
            return t["id"]

    tag = cv_api("POST", f"projects/{project_id}/tags?name={tag_name}", api_key)
    print(f"  Created tag: {tag['name']} ({tag['id']})")
    return tag["id"]


def get_existing_image_count(api_key: str, project_id: str) -> int:
    """Get how many images are already in the project."""
    try:
        # The imageCount is available via project details
        project = cv_api("GET", f"projects/{project_id}", api_key)
        return project.get("imageCount", 0)
    except Exception:
        return 0


def upload_images_with_regions(api_key: str, project_id: str, tag_id: str,
                                entries: list):
    """Upload images with bounding box regions in batches of 64.

    Uses POST /images/files with base64-encoded image content and regions.
    """
    import base64

    total = len(entries)
    batch_size = 64
    uploaded = 0
    failed = 0

    print(f"\nUploading {total} images with regions (batch size {batch_size})...")

    for batch_start in range(0, total, batch_size):
        batch = entries[batch_start:batch_start + batch_size]
        images_data = []

        for img_path, regions in batch:
            with open(img_path, "rb") as f:
                img_bytes = f.read()

            regions_list = [{
                "tagId": tag_id,
                "left": r["left"],
                "top": r["top"],
                "width": r["width"],
                "height": r["height"],
            } for r in regions]

            images_data.append({
                "name": os.path.basename(img_path),
                "contents": base64.b64encode(img_bytes).decode(),
                "regions": regions_list,
            })

        body = {"images": images_data}

        try:
            result = cv_api("POST",
                          f"projects/{project_id}/images/files",
                          api_key, body)
            if isinstance(result, dict):
                ok = len([i for i in result.get("images", [])
                         if i.get("status") in ("OK", "OKDuplicate")])
                err = len(batch) - ok
                uploaded += ok
                failed += err
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  Rate limited, waiting 60s...")
                time.sleep(60)
                try:
                    result = cv_api("POST",
                                  f"projects/{project_id}/images/files",
                                  api_key, body)
                    if isinstance(result, dict):
                        ok = len([i for i in result.get("images", [])
                                 if i.get("status") in ("OK", "OKDuplicate")])
                        uploaded += ok
                        failed += len(batch) - ok
                except Exception:
                    failed += len(batch)
            else:
                failed += len(batch)

        done = batch_start + len(batch)
        if done % 256 == 0 or done == total or done <= batch_size:
            print(f"  [{done}/{total}] uploaded={uploaded} failed={failed}")

    print(f"  Upload complete: {uploaded} succeeded, {failed} failed")
    return uploaded


def train_project(api_key: str, project_id: str) -> str:
    """Start training and return iteration ID."""
    print("\nStarting training...")
    result = cv_api("POST", f"projects/{project_id}/train", api_key)
    iteration_id = result.get("id", "unknown")
    print(f"  Training started: iteration {iteration_id}")
    print(f"  Status: {result.get('status', 'unknown')}")
    return iteration_id


def publish_iteration(api_key: str, project_id: str, iteration_id: str):
    """Publish a trained iteration for prediction."""
    prediction_resource_id = _az(
        "cognitiveservices", "account", "show",
        "--name", PREDICTION_RESOURCE,
        "--resource-group", RESOURCE_GROUP,
        "--query", "id", "-o", "tsv",
    )
    publish_name = "antenna-detector-latest"
    try:
        cv_api("POST",
              f"projects/{project_id}/iterations/{iteration_id}/publish"
              f"?publishName={publish_name}"
              f"&predictionId={prediction_resource_id}",
              api_key)
        print(f"  Published as: {publish_name}")
    except Exception as e:
        print(f"  Publish failed (training may still be running): {e}")


def check_training_status(api_key: str):
    """Check project and training status."""
    projects = cv_api("GET", "projects", api_key)

    if not projects:
        print("No projects found.")
        return

    for p in projects:
        print(f"\n--- Project: {p['name']} ---")
        print(f"  ID: {p['id']}")
        project_id = p["id"]

        # Get iterations
        try:
            iterations = cv_api("GET", f"projects/{project_id}/iterations", api_key)
            if not iterations:
                print("  No training iterations yet.")
                continue

            for it in iterations:
                print(f"\n  Iteration: {it['id']}")
                print(f"    Status: {it.get('status', 'unknown')}")
                print(f"    Created: {it.get('created', 'unknown')}")
                print(f"    Trained At: {it.get('trainedAt', 'not yet')}")

                if it.get("status") == "Completed":
                    # Get performance
                    try:
                        perf = cv_api("GET",
                                     f"projects/{project_id}/iterations/{it['id']}/performance",
                                     api_key)
                        print(f"    Precision: {perf.get('precision', 'N/A')}")
                        print(f"    Recall: {perf.get('recall', 'N/A')}")
                        print(f"    mAP: {perf.get('averagePrecision', 'N/A')}")
                    except Exception:
                        pass

                    if it.get("publishName"):
                        print(f"    Published as: {it['publishName']}")
                    else:
                        print("    Not published yet.")
        except Exception as e:
            print(f"  Error getting iterations: {e}")


# ── Main Pipeline ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Train antenna detection model on Azure Custom Vision"
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Path to existing dataset (skip download)",
    )
    parser.add_argument(
        "--max-images", type=int, default=MAX_IMAGES,
        help=f"Max images total (default {MAX_IMAGES}, F0 tier limit)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Just check training status and exit",
    )
    args = parser.parse_args()

    api_key = get_training_key()
    print(f"Training endpoint: {TRAINING_ENDPOINT}")

    if args.status:
        check_training_status(api_key)
        return

    # Step 1: Get dataset
    if args.data_dir:
        dataset_root = args.data_dir
    else:
        dataset_root = os.path.join("datasets", "antenna-dataset")
        download_dataset(dataset_root)

    # Step 2: Load YOLO annotations
    print("\nLoading YOLO annotations...")
    entries = load_yolo_annotations(dataset_root, max_images=args.max_images)

    # Step 3: Create Custom Vision project and tag
    print("\nSetting up Custom Vision project...")
    project_id = get_or_create_project(api_key)
    tag_id = get_or_create_tag(api_key, project_id, "cellular_antenna")

    # Check existing images
    existing = get_existing_image_count(api_key, project_id)
    if existing > 0:
        print(f"  Project already has {existing} images")
        if existing >= len(entries):
            print("  Skipping upload (already populated)")
            entries = []

    # Step 4: Upload images with regions
    if entries:
        uploaded = upload_images_with_regions(api_key, project_id, tag_id, entries)
        if uploaded == 0:
            print("ERROR: No images uploaded. Cannot train.")
            return

    # Step 5: Train
    try:
        iteration_id = train_project(api_key, project_id)
    except Exception as e:
        print(f"  Training failed to start: {e}")
        return

    # Step 6: Wait briefly and check status
    print("\nWaiting 10s then checking status...")
    time.sleep(10)
    check_training_status(api_key)

    print(f"\n{'='*60}")
    print("Training is running in Azure Custom Vision.")
    print(f"Check status:  python train_azure_vision.py --status")
    print(f"Portal:        https://www.customvision.ai/projects")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
