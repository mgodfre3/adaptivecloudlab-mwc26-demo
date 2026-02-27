FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY drone-telemetry-simulator.py .

# Env vars injected via K8s Secret / ConfigMap
ENV DRONE_COUNT=5
ENV SEND_INTERVAL_SECONDS=5

CMD ["python", "drone-telemetry-simulator.py"]
