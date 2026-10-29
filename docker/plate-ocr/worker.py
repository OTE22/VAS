"""Isolated OCR pilot. Loopback only; no production queue, DB or camera access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
import logging
import time
import warnings
import hmac
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from PIL import Image

MODELS = {
    'ar': 'arabic_PP-OCRv5_mobile_rec',
    'en': 'en_PP-OCRv5_mobile_rec',
    'latin': 'latin_PP-OCRv5_mobile_rec',
}
DETECTOR = 'PP-OCRv5_mobile_det'
MODEL_ROOT = '/opt/plate-ocr/models'
PILOT_CAMERA = 'IT-DIRECT-CAM-1'
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 1280 * 720
MAX_SIDE = 1280
QUEUE_SIZE = 4
QUEUE_WAIT_SECONDS = 10
MIN_JOB_INTERVAL = .25
log = logging.getLogger('plate_ocr')


def decode_crop(payload):
    """Inspect dimensions before allocating pixels; OCR accepts plate crops only."""
    if not payload or len(payload) > MAX_BYTES:
        raise ValueError('Provide a JPEG/PNG plate crop of at most 2 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as source:
                width, height = source.size
                if source.format not in ('JPEG', 'PNG'):
                    raise ValueError('Only JPEG and PNG are supported.')
                if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                    raise ValueError('Plate crop exceeds the 1280-side / 921600-pixel limit.')
                if min(width, height) < 8:
                    raise ValueError('Plate crop is too small.')
                return source.convert('RGB')
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError('Invalid or oversized plate crop.') from exc


class OCRRuntime:
    def __init__(self):
        import cv2
        import paddle
        from paddleocr import TextDetection, TextRecognition
        import numpy as np
        cv2.setNumThreads(1)
        if not paddle.is_compiled_with_cuda() or paddle.device.cuda.device_count() != 1:
            raise RuntimeError('Exactly one visible CUDA GPU is required; CPU fallback is disabled.')
        paddle.set_device('gpu:0')
        probe = paddle.to_tensor([2.0, 3.0])
        assert float((probe * probe).sum().numpy()) == 13.0
        self.paddle = paddle
        common = dict(device='gpu:0', cpu_threads=2, enable_hpi=False, use_tensorrt=False)
        self.detector = TextDetection(model_name=DETECTOR, model_dir=f'{MODEL_ROOT}/{DETECTOR}',
                                      limit_side_len=640, limit_type='max', **common)
        self.readers = {key: TextRecognition(model_name=name, model_dir=f'{MODEL_ROOT}/{name}', **common)
                        for key, name in MODELS.items()}
        # Execute every model on the selected GPU before announcing readiness.
        blank = np.full((64, 320, 3), 255, dtype=np.uint8)
        list(self.detector.predict(blank, batch_size=1))
        for model in self.readers.values():
            list(model.predict(blank, batch_size=1))
        paddle.device.synchronize()
        self.gpu_name = paddle.device.cuda.get_device_name(0)

    def infer(self, image, profile):
        import cv2
        import numpy as np
        started = time.monotonic()
        frame = np.asarray(image)[:, :, ::-1].copy()  # Paddle's ndarray API uses BGR.
        detection = list(self.detector.predict(frame, batch_size=1))[0]
        boxes = detection['dt_polys']
        if len(boxes) > 12:
            raise ValueError('Too many text regions; supply a tightly cropped plate image.')
        lines = []
        for points in sorted(boxes, key=lambda p: (float(p[:, 1].mean()), float(p[:, 0].mean()))):
            points = np.asarray(points, dtype=np.float32)
            if points.shape != (4, 2):
                continue
            width = int(max(np.linalg.norm(points[0]-points[1]), np.linalg.norm(points[2]-points[3])))
            height = int(max(np.linalg.norm(points[0]-points[3]), np.linalg.norm(points[1]-points[2])))
            if min(width, height) < 2:
                continue
            width, height = min(width, MAX_SIDE), min(height, MAX_SIDE)
            target = np.array([[0, 0], [width-1, 0], [width-1, height-1], [0, height-1]], np.float32)
            crop = cv2.warpPerspective(frame, cv2.getPerspectiveTransform(points, target), (width, height))
            result = list(self.readers[profile].predict(crop, batch_size=1))[0]
            lines.append({'text': str(result['rec_text']), 'score': float(result['rec_score']),
                          'polygon': points.tolist()})
        self.paddle.device.synchronize()
        return {'profile': profile, 'model': MODELS[profile], 'device': 'gpu:0',
                'lines': lines, 'review_required': True,
                'inference_ms': round((time.monotonic()-started)*1000, 2)}


@dataclass
class Job:
    image: Image.Image
    profile: str
    future: asyncio.Future
    enqueued: float


class Controller:
    def __init__(self, runtime):
        self.runtime = runtime
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='plate-gpu')
        self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.ready = False
        self.completed = 0
        self.failed = 0
        self.busy_since = None
        self.last_started = 0.0

    async def initialize(self):
        self.runtime = await asyncio.get_running_loop().run_in_executor(self.pool, self.runtime)
        self.ready = True

    async def consume(self):
        loop = asyncio.get_running_loop()
        while True:
            job = await self.queue.get()
            try:
                await asyncio.sleep(max(0, MIN_JOB_INTERVAL - (time.monotonic()-self.last_started)))
                if job.future.cancelled():
                    continue
                if not self.ready:
                    job.future.set_exception(HTTPException(503, 'OCR unavailable.'))
                    continue
                if time.monotonic()-job.enqueued > QUEUE_WAIT_SECONDS:
                    job.future.set_exception(HTTPException(503, 'OCR job expired before processing.'))
                    continue
                self.busy_since = self.last_started = time.monotonic()
                result = await loop.run_in_executor(self.pool, self.runtime.infer, job.image, job.profile)
                self.completed += 1
                if not job.future.done():
                    job.future.set_result(result)
            except ValueError as exc:
                if not job.future.done():
                    job.future.set_exception(HTTPException(422, str(exc)))
            except Exception:
                self.failed += 1
                self.ready = False
                log.exception('GPU OCR failed; worker requires inspection/restart')
                if not job.future.done():
                    job.future.set_exception(HTTPException(503, 'OCR unavailable.'))
            finally:
                self.busy_since = None
                job.image.close()
                self.queue.task_done()


@asynccontextmanager
async def lifespan(app):
    controller = Controller(OCRRuntime)
    app.state.controller = controller
    await controller.initialize()
    task = asyncio.create_task(controller.consume())
    try:
        yield
    finally:
        controller.ready = False
        await controller.queue.join()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        controller.pool.shutdown(wait=True)


app = FastAPI(title='VAS isolated plate OCR pilot', lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def authenticate_worker(request, call_next):
    token_file = os.environ.get('VEHICLE_WORKER_TOKEN_FILE')
    if token_file and request.url.path != '/health/ready':
        from fastapi.responses import JSONResponse
        token = Path(token_file).read_text().strip()
        if len(token) < 32 or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + token):
            return JSONResponse({'detail': 'Worker authentication required'}, status_code=401)
    return await call_next(request)


@app.get('/health/ready')
async def health():
    c = app.state.controller
    if not c.ready or (c.busy_since and time.monotonic()-c.busy_since > 30):
        raise HTTPException(503, 'OCR is unavailable or stalled.')
    return {'status': 'ready', 'device': 'gpu:0', 'gpu': c.runtime.gpu_name,
            'pilot_camera': PILOT_CAMERA, 'live_ingestion': False,
            'models': MODELS, 'detector': DETECTOR, 'queue_depth': c.queue.qsize(),
            'queue_limit': QUEUE_SIZE, 'completed': c.completed, 'failed': c.failed}


@app.post('/recognize')
async def recognize(request: Request, profile: str = 'ar', camera: str = PILOT_CAMERA):
    if camera != PILOT_CAMERA:
        raise HTTPException(403, 'This pilot is scoped to IT-DIRECT-CAM-1.')
    if profile not in MODELS:
        raise HTTPException(422, 'Select ar, en, or latin explicitly.')
    c = app.state.controller
    if not c.ready:
        raise HTTPException(503, 'OCR unavailable.')
    if c.queue.full():
        raise HTTPException(429, 'OCR queue is full; retry later.', headers={'Retry-After': '2'})
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_BYTES:
            raise HTTPException(413, 'Plate crop exceeds 2 MB.')
    try:
        image = decode_crop(payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    future = asyncio.get_running_loop().create_future()
    try:
        c.queue.put_nowait(Job(image, profile, future, time.monotonic()))
    except asyncio.QueueFull:
        image.close()
        raise HTTPException(429, 'OCR queue is full; retry later.', headers={'Retry-After': '2'})
    try:
        return await asyncio.wait_for(future, timeout=30)
    except asyncio.TimeoutError:
        # An active GPU call cannot be cancelled. The single consumer keeps its
        # slot until it finishes; queued cancelled jobs are discarded safely.
        raise HTTPException(504, 'OCR timed out.')
