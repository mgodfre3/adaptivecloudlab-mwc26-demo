#!/usr/bin/env python3
"""Submit YOLOv8 antenna training to Azure ML with GPU compute.

This script wraps the local train_tower_model.py workflow and submits it
as an Azure ML job on a GPU compute cluster for fast training (~30 min
on NC-series vs 18+ hours on CPU).

Workflow:
  1. Connect to Azure ML workspace
  2. Create/reuse GPU compute cluster (NC6s_v3 by default)
  3. Create a training environment with ultralytics + ONNX deps
  4. Submit training job that runs train_tower_model.py
  5. Stream logs and download the resulting ONNX model

Usage:
    # Submit training job to Azure ML (uses Demo-AML workspace)
    python train_azureml.py

    # Custom workspace / compute
    python train_azureml.py \\
        --subscription fbaf508b-cb61-4383-9cda-a42bfa0c7bc9 \\
        --resource-group kkambow-rg \\
        --workspace Demo-AML \\
        --compute-size Standard_NC6s_v3

    # Download model from a completed job
    python train_azureml.py --download-from <job-name>

Prerequisites:
    pip install azure-ai-ml azure-identity

After training:
    The ONNX model is downloaded to ./yolov8s-antenna.onnx.
    Deploy to the edge cluster with:
      kubectl cp yolov8s-antenna.onnx video-analysis/<pod>:/data/models/yolov8s-antenna.onnx
      kubectl rollout restart deployment/video-dashboard -n video-analysis
"""

import argparse
import os
import sys
import time


def get_ml_client(subscription_id: str, resource_group: str, workspace: str):
    """Create an Azure ML client."""
    from azure.ai.ml import MLClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential()
    return MLClient(credential, subscription_id, resource_group, workspace)


def ensure_compute(ml_client, compute_name: str, vm_size: str, max_instances: int = 1):
    """Create GPU compute cluster if it doesn't already exist."""
    from azure.ai.ml.entities import AmlCompute

    try:
        compute = ml_client.compute.get(compute_name)
        print(f"✅ Compute '{compute_name}' exists (size={compute.size}, state={compute.state})")
        return compute
    except Exception:
        pass

    print(f"📦 Creating compute cluster '{compute_name}' ({vm_size}, max={max_instances})...")
    compute = AmlCompute(
        name=compute_name,
        type="amlcompute",
        size=vm_size,
        min_instances=0,
        max_instances=max_instances,
        idle_time_before_scale_down=600,
        tier="Dedicated",
    )
    ml_client.compute.begin_create_or_update(compute).result()
    print(f"✅ Compute '{compute_name}' created")
    return compute


def create_environment(ml_client, env_name: str = "yolov8-training"):
    """Create a curated training environment with CUDA + ultralytics."""
    from azure.ai.ml.entities import Environment

    # Check if environment already exists
    try:
        env = ml_client.environments.get(env_name, label="latest")
        print(f"✅ Environment '{env_name}' exists (version={env.version})")
        return env
    except Exception:
        pass

    print(f"📦 Creating environment '{env_name}'...")

    conda_yaml = """
name: yolov8-train
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11
  - pip
  - pip:
    - ultralytics>=8.2.0
    - onnx>=1.16.0
    - onnxruntime>=1.18.0
    - gdown>=5.1.0
    - pyyaml>=6.0
    - opencv-python-headless>=4.9.0
    - numpy>=1.26.0
    - torch>=2.2.0
    - torchvision>=0.17.0
"""
    # Write temp conda file
    conda_path = os.path.join(os.path.dirname(__file__), "_azureml_conda.yml")
    with open(conda_path, "w") as f:
        f.write(conda_yaml)

    env = Environment(
        name=env_name,
        description="YOLOv8 training environment with CUDA support for cell tower detection",
        image="mcr.microsoft.com/azureml/curated/acpt-pytorch-2.2-cuda12.1:latest",
        conda_file=conda_path,
    )
    env = ml_client.environments.create_or_update(env)
    os.remove(conda_path)
    print(f"✅ Environment '{env_name}' created (version={env.version})")
    return env


def submit_training_job(
    ml_client,
    compute_name: str,
    env_name: str,
    epochs: int = 30,
    batch: int = 16,
    experiment_name: str = "antenna-detection",
):
    """Submit a training job to Azure ML."""
    from azure.ai.ml import Input, command
    from azure.ai.ml.constants import AssetTypes

    print(f"🚀 Submitting training job (epochs={epochs}, batch={batch})...")

    # The training command downloads the dataset, trains, and exports
    training_command = (
        "pip install gdown rarfile && "
        "python train_tower_model.py "
        f"--epochs {epochs} --batch {batch} "
        "--output ${{outputs.model_output}}/yolov8s-antenna.onnx"
    )

    job = command(
        code=os.path.dirname(__file__) or ".",
        command=training_command,
        environment=f"{env_name}@latest",
        compute=compute_name,
        outputs={
            "model_output": {"type": AssetTypes.URI_FOLDER, "mode": "rw_mount"},
        },
        display_name=f"yolov8s-antenna-{epochs}ep",
        experiment_name=experiment_name,
        description=(
            "Fine-tune YOLOv8s on Antenna-Dataset (9,156 images) "
            "for cell tower detection. Exports ONNX for edge deployment."
        ),
        tags={
            "model": "yolov8s",
            "dataset": "jafaryi/Antenna-Dataset",
            "classes": "1",
            "task": "object-detection",
            "target": "edge-deployment",
        },
    )

    returned_job = ml_client.jobs.create_or_update(job)
    print(f"✅ Job submitted: {returned_job.name}")
    print(f"   Studio URL: {returned_job.studio_url}")
    print(f"   Status: {returned_job.status}")
    return returned_job


def wait_for_job(ml_client, job_name: str, poll_interval: int = 30):
    """Wait for a job to complete and return the final status."""
    print(f"⏳ Waiting for job '{job_name}' to complete...")
    print(f"   (polling every {poll_interval}s — press Ctrl+C to stop waiting)")

    terminal_states = {"Completed", "Failed", "Canceled", "NotResponding"}
    start = time.time()

    while True:
        job = ml_client.jobs.get(job_name)
        elapsed = int(time.time() - start)
        mins, secs = divmod(elapsed, 60)

        if job.status in terminal_states:
            print(f"\n{'✅' if job.status == 'Completed' else '❌'} Job {job.status} ({mins}m {secs}s)")
            return job

        print(f"   [{mins:02d}:{secs:02d}] Status: {job.status}", end="\r")
        time.sleep(poll_interval)


def download_model(ml_client, job_name: str, output_path: str = "yolov8s-antenna.onnx"):
    """Download the ONNX model from a completed job."""
    print(f"📥 Downloading model from job '{job_name}'...")

    download_dir = os.path.join(os.path.dirname(__file__) or ".", "_azureml_output")
    os.makedirs(download_dir, exist_ok=True)

    ml_client.jobs.download(job_name, output_name="model_output", download_path=download_dir)

    # Find the ONNX file
    for root, _, files in os.walk(download_dir):
        for f in files:
            if f.endswith(".onnx"):
                src = os.path.join(root, f)
                import shutil
                shutil.copy2(src, output_path)
                print(f"✅ Model saved: {output_path}")
                # Clean up
                shutil.rmtree(download_dir, ignore_errors=True)
                return output_path

    print("❌ No ONNX file found in job outputs")
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Train YOLOv8 antenna model on Azure ML with GPU"
    )
    parser.add_argument(
        "--subscription", default="fbaf508b-cb61-4383-9cda-a42bfa0c7bc9",
        help="Azure subscription ID",
    )
    parser.add_argument(
        "--resource-group", default="kkambow-rg",
        help="Azure ML resource group",
    )
    parser.add_argument(
        "--workspace", default="Demo-AML",
        help="Azure ML workspace name",
    )
    parser.add_argument(
        "--compute-name", default="gpu-nc6sv3",
        help="Compute cluster name",
    )
    parser.add_argument(
        "--compute-size", default="Standard_NC6s_v3",
        help="VM size for compute cluster",
    )
    parser.add_argument("--epochs", type=int, default=30, help="Training epochs")
    parser.add_argument("--batch", type=int, default=16, help="Batch size")
    parser.add_argument(
        "--output", default="yolov8s-antenna.onnx",
        help="Output ONNX filename",
    )
    parser.add_argument(
        "--download-from", default=None,
        help="Skip training; download model from this job name",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Submit job and exit without waiting",
    )

    args = parser.parse_args()

    # Connect to workspace
    print(f"🔗 Connecting to Azure ML workspace: {args.workspace}")
    ml_client = get_ml_client(args.subscription, args.resource_group, args.workspace)
    ws = ml_client.workspaces.get(args.workspace)
    print(f"   Workspace: {ws.name} ({ws.location})")

    # Download-only mode
    if args.download_from:
        download_model(ml_client, args.download_from, args.output)
        return

    # Ensure compute
    ensure_compute(ml_client, args.compute_name, args.compute_size)

    # Ensure environment
    create_environment(ml_client)

    # Submit job
    job = submit_training_job(
        ml_client,
        compute_name=args.compute_name,
        env_name="yolov8-training",
        epochs=args.epochs,
        batch=args.batch,
    )

    if args.no_wait:
        print(f"\n📋 Job submitted. Track at: {job.studio_url}")
        print(f"   Download later: python train_azureml.py --download-from {job.name}")
        return

    # Wait and download
    completed_job = wait_for_job(ml_client, job.name)
    if completed_job.status == "Completed":
        download_model(ml_client, completed_job.name, args.output)
        print(f"\n🚀 Ready for deployment: {args.output}")
        print(f"   kubectl cp {args.output} video-analysis/<pod>:/data/models/yolov8s-antenna.onnx")
    else:
        print(f"\n❌ Job failed. Check logs at: {completed_job.studio_url}")
        sys.exit(1)


if __name__ == "__main__":
    main()
