FROM armyeye-vms
USER root
RUN python -m pip install --no-cache-dir omegaconf==2.3.0 gunicorn==23.0.0
WORKDIR /opt/vehicle-vision
COPY worker.py ./worker.py
COPY --chown=1000:1000 models/ ./models/
COPY models-manifest.json ./models-manifest.json
RUN python -c 'import json,hashlib; from pathlib import Path; m=json.loads(Path("models-manifest.json").read_text()); assert all(hashlib.sha256((Path("models")/n).read_bytes()).hexdigest()==h for n,h in m.items())'
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 YOLO_AUTOINSTALL=false OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 YOLO_CONFIG_DIR=/tmp/yolo HOME=/tmp/vehicle-home
USER 1000:1000
ENTRYPOINT []
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8092", "--workers", "1", "--threads", "2", "--timeout", "90", "--worker-tmp-dir", "/tmp", "worker:app"]
