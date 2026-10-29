"""One GPU inference at a time: vehicle descriptors, plate localization, OCR."""
import base64
import hmac
from io import BytesIO
import json
import os
from pathlib import Path
import threading
import time
import urllib.request

os.environ['YOLO_AUTOINSTALL'] = 'false'
from flask import Flask, request, jsonify
from PIL import Image, ImageOps
import numpy as np
import torch
from ultralytics import YOLO

VERSION = 'yolov8n-layer21-256-v1'
TOKEN = Path(os.environ['VEHICLE_WORKER_TOKEN_FILE']).read_text().strip()
if len(TOKEN) < 32:
    raise RuntimeError('Worker token is too short.')
if not torch.cuda.is_available():
    raise RuntimeError('CUDA is required; CPU fallback is disabled.')
torch.set_num_threads(2)
torch.set_num_interop_threads(1)
plates = YOLO('/opt/vehicle-vision/models/LPR.pt')
appearance = YOLO('/opt/vehicle-vision/models/yolov8n.pt')
lock = threading.Lock()
ready = True
completed = 0
failed = 0
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024


def encode(image):
    out = BytesIO()
    image.save(out, 'JPEG', quality=95)
    return out.getvalue()


def descriptor(frame):
    result = appearance.predict(frame, embed=[21], imgsz=224, device=0, half=True, verbose=False)[0]
    vector = result.float().detach().cpu().numpy().reshape(-1)
    if vector.shape != (256,) or not np.isfinite(vector).all():
        raise RuntimeError('Invalid vehicle descriptor.')
    vector /= max(float(np.linalg.norm(vector)), 1e-12)
    return vector.tolist()


# Warm both GPU models before serving requests.
blank = np.zeros((224, 224, 3), dtype=np.uint8)
descriptor(blank)
plates.predict(blank, imgsz=640, device=0, half=True, verbose=False)
torch.cuda.synchronize()


@app.before_request
def authenticate():
    if request.path != '/health/ready' and not hmac.compare_digest(
            request.headers.get('Authorization', ''), 'Bearer ' + TOKEN):
        return jsonify(error='Authentication required'), 401


@app.get('/health/ready')
def health():
    return jsonify(status='ready' if ready else 'unavailable', device='cuda:0',
                   completed=completed, failed=failed, embedding_version=VERSION), 200 if ready else 503


@app.post('/embedding')
@app.post('/analyze')
def analyze():
    global ready, completed, failed
    if not ready:
        return jsonify(error='Worker unavailable'), 503
    profile = request.args.get('profile', 'en')
    if profile not in ('ar', 'en', 'latin'):
        return jsonify(error='Invalid OCR profile'), 422
    if not lock.acquire(blocking=False):
        return jsonify(error='GPU busy'), 429, {'Retry-After': '1'}
    try:
        try:
            with Image.open(BytesIO(request.get_data())) as source:
                if source.format not in ('JPEG', 'PNG') or source.width * source.height > 4_000_000:
                    return jsonify(error='Invalid or oversized vehicle crop'), 422
                image = ImageOps.exif_transpose(source).convert('RGB')
        except (ValueError, OSError):
            return jsonify(error='Invalid image'), 422
        frame = np.asarray(image)[:, :, ::-1].copy()
        start = time.monotonic()
        result = {'embedding': descriptor(frame), 'embedding_version': VERSION, 'plates': []}
        if request.path == '/analyze':
            detection = plates.predict(frame, imgsz=640, device=0, half=True,
                                       conf=.25, max_det=3, verbose=False)[0]
            for box in sorted(detection.boxes, key=lambda b: float(b.conf[0]), reverse=True):
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(image.width, x2), min(image.height, y2)
                if min(x2-x1, y2-y1) < 8:
                    continue
                crop = image.crop((x1, y1, x2, y2))
                crop.thumbnail((1280, 720))
                data = encode(crop)
                req = urllib.request.Request(os.environ['PLATE_OCR_URL'].rstrip('/') + '/recognize?profile=' + profile,
                    data=data, headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'image/jpeg'})
                with urllib.request.urlopen(req, timeout=35) as response:
                    ocr = json.load(response)
                result['plates'].append({'bbox': [x1, y1, x2, y2], 'detection_score': float(box.conf[0]),
                                         'crop_b64': base64.b64encode(data).decode(), 'ocr': ocr})
        torch.cuda.synchronize()
        completed += 1
        result['inference_ms'] = round((time.monotonic()-start)*1000, 2)
        return jsonify(result)
    except urllib.error.HTTPError as exc:
        return jsonify(error='OCR unavailable', upstream_status=exc.code), 503
    except (urllib.error.URLError, TimeoutError):
        return jsonify(error='OCR unavailable'), 503
    except Exception:
        failed += 1
        ready = False
        app.logger.exception('Vehicle GPU inference failed')
        return jsonify(error='GPU unavailable'), 503
    finally:
        lock.release()
