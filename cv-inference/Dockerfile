FROM nvcr.io/nvidia/cuda:12.2.0-runtime-ubuntu22.04

LABEL maintainer="Adaptive Cloud Lab" \
      description="YOLOv8 antenna detection inference pipeline" \
      version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    ORT_TENSORRT_CACHE_PATH=/models/cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        libgl1-mesa-glx \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY inference.py postprocess.py annotate.py export_geojson.py ./

RUN mkdir -p /models /data/output

ENTRYPOINT ["python3", "inference.py"]
