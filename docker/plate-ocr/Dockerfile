FROM python:3.11-slim AS ocr-deps

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_PDX_MODEL_SOURCE=BOS \
    FLAGS_allocator_strategy=auto_growth \
    HF_HUB_OFFLINE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 libglib2.0-0 libgl1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --only-binary=:all: --timeout 120 --retries 5 \
    paddleocr==3.7.0 paddlex==3.7.2 fastapi==0.121.3 uvicorn==0.38.0 \
    aiohttp==3.13.3 yarl==1.22.0

FROM ocr-deps AS runtime
ENV MKL_NUM_THREADS=2 NUMEXPR_MAX_THREADS=2
RUN --mount=type=bind,source=wheels,target=/wheels \
    --mount=type=bind,source=runtime-lock.json,target=/runtime-lock.json \
    --mount=type=bind,source=cuda-lock.json,target=/cuda-lock.json \
    python -c 'import json,hashlib; from pathlib import Path; m=json.loads(Path("/runtime-lock.json").read_text()); f=open("/wheels/"+m["filename"],"rb"); assert hashlib.file_digest(f,"sha256").hexdigest()==m["sha256"]' \
    && python -c 'import json,hashlib; from pathlib import Path; models=json.loads(Path("/cuda-lock.json").read_text()); exec("for m in models:\n with open(\"/wheels/\"+m[\"filename\"],\"rb\") as f: assert hashlib.file_digest(f,\"sha256\").hexdigest()==m[\"sha256\"]")' \
    && python -m pip install --no-cache-dir --no-index --no-deps /wheels/nvidia_*.whl \
    && python -m pip install --no-cache-dir /wheels/paddlepaddle_gpu-3.3.1-cp311-cp311-linux_x86_64.whl

WORKDIR /opt/plate-ocr
COPY *.py ./
COPY fixtures/ ./fixtures/
COPY wheels/models/ ./models/
COPY models-lock.json ./models-lock.json
RUN python verify_bundle.py \
    && python -m pip freeze > /opt/plate-ocr/installed-packages.txt \
    && useradd --uid 1000 --create-home ocr
USER 1000:1000
ENV HOME=/tmp/ocr-home OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
CMD ["python", "-m", "uvicorn", "worker:app", "--host", "127.0.0.1", "--port", "8091", "--workers", "1", "--limit-concurrency", "8", "--no-access-log"]
