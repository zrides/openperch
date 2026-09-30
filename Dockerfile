# Kiwibit Hub: web UI + FastAPI + BioCLIP/YOLO-World on CUDA 12.8 + live relay.
# The PyTorch wheels bundle the CUDA runtime; the host only needs an NVIDIA
# driver and the NVIDIA container runtime (Docker Desktop + WSL2 on Windows).

# ---- web UI -------------------------------------------------------------------
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---- live relay ---------------------------------------------------------------
FROM golang:1.25-bookworm AS relay
WORKDIR /src
COPY live/go.mod live/go.sum ./
RUN go mod download
COPY live/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/kiwibit-relay ./cmd/relay

# ---- hub ----------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KIWIBIT_DATA_DIR=/data \
    KIWIBIT_BIOCLIP_DIR=/models/bioclip \
    KIWIBIT_ACCOUNT_CONFIG=/run/secrets/kiwibit_account \
    KIWIBIT_WEB_DIR=/app/web \
    KIWIBIT_LIVE_RELAY=/usr/local/bin/kiwibit-relay

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Heavy, rarely-changing layers first.
RUN pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128

# Install dependencies from pyproject with a stub package so source edits don't
# invalidate this layer.
COPY backend/pyproject.toml ./
RUN mkdir kiwibit_hub && touch kiwibit_hub/__init__.py \
    && pip install ".[ml]" \
    # ultralytics pulls the GUI build of OpenCV; the server wants the headless one only.
    && pip uninstall -y opencv-python opencv-python-headless \
    && pip install "opencv-python-headless>=4.10,<5" \
    && rm -rf kiwibit_hub

COPY backend/kiwibit_hub ./kiwibit_hub
RUN pip install --no-deps . \
    && useradd --uid 1000 --create-home hub \
    && mkdir -p /data /models \
    && chown hub:hub /data
COPY --from=relay /out/kiwibit-relay /usr/local/bin/kiwibit-relay
COPY --from=web /web/dist /app/web
COPY LICENSE /app/LICENSE

USER hub
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Exactly one process: it owns the GPU models, the job queue and the live relay.
CMD ["uvicorn", "--factory", "kiwibit_hub.server:build_app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
