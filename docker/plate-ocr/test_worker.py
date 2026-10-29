"""Small isolated checks; no production database, camera, or GPU required."""
import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
import time
import unittest
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from worker import app, Controller, Job, decode_crop, QUEUE_SIZE


def png(size=(300, 80)):
    buf = BytesIO()
    Image.new('RGB', size, 'white').save(buf, format='PNG')
    return buf.getvalue()


class Runtime:
    gpu_name = 'fake-for-unit-test'

    def infer(self, image, profile):
        return {'lines': [], 'profile': profile}


@asynccontextmanager
async def fake_lifespan(application):
    c = Controller(Runtime)
    await c.initialize()
    application.state.controller = c
    task = asyncio.create_task(c.consume())
    try:
        yield
    finally:
        await c.queue.join()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        c.pool.shutdown()


class WorkerTests(unittest.TestCase):
    def test_reject_bad_or_oversized_images_before_inference(self):
        for value in (b'', b'not an image', png((1281, 20)), png((1280, 721))):
            with self.assertRaises(ValueError):
                decode_crop(value)
        image = decode_crop(png())
        self.assertEqual(image.size, (300, 80))
        image.close()

    def test_api_rejects_wrong_camera_profile_and_large_body(self):
        old = app.router.lifespan_context
        app.router.lifespan_context = fake_lifespan
        try:
            with TemporaryDirectory() as directory:
                token = Path(directory) / 'token'
                token.write_text('unit-test-token-long-enough-for-auth')
                with patch.dict('os.environ', {'VEHICLE_WORKER_TOKEN_FILE': str(token)}), TestClient(app) as client:
                    self.assertEqual(client.post('/recognize', content=png()).status_code, 401)
                    client.headers['Authorization'] = 'Bearer wrong'
                    self.assertEqual(client.post('/recognize', content=png()).status_code, 401)
                    client.headers['Authorization'] = 'Bearer unit-test-token-long-enough-for-auth'
                    self.check_authenticated_api(client)
        finally:
            app.router.lifespan_context = old

    def check_authenticated_api(self, client):
        self.assertEqual(client.get('/health/ready').status_code, 200)
        self.assertEqual(client.post('/recognize?camera=OTHER', content=png()).status_code, 403)
        self.assertEqual(client.post('/recognize?profile=auto', content=png()).status_code, 422)
        self.assertEqual(client.post('/recognize', content=b'invalid').status_code, 422)
        self.assertEqual(client.post('/recognize', content=b'x'*(2*1024*1024+1)).status_code, 413)
        result = client.post('/recognize?profile=en', content=png())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['profile'], 'en')

    def test_queue_is_bounded_expired_jobs_do_not_run(self):
        async def check():
            c = Controller(Runtime)
            await c.initialize()
            loop = asyncio.get_running_loop()
            futures = []
            for _ in range(QUEUE_SIZE):
                f = loop.create_future()
                futures.append(f)
                c.queue.put_nowait(Job(Image.new('RGB', (20, 20)), 'en', f, time.monotonic()-20))
            with self.assertRaises(asyncio.QueueFull):
                c.queue.put_nowait(None)
            task = asyncio.create_task(c.consume())
            for f in futures:
                with self.assertRaises(HTTPException) as failure:
                    await f
                self.assertEqual(failure.exception.status_code, 503)
            await c.queue.join()
            self.assertEqual(c.completed, 0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            c.pool.shutdown()
        asyncio.run(check())

    def test_inference_is_serial_even_when_first_client_cancels(self):
        async def check():
            import threading
            started, release = threading.Event(), threading.Event()
            calls = []
            class Slow(Runtime):
                def infer(self, image, profile):
                    calls.append(profile)
                    if profile == 'ar':
                        started.set()
                        release.wait(3)
                    return {'profile': profile}
            c = Controller(Slow)
            await c.initialize()
            loop = asyncio.get_running_loop()
            first, second = loop.create_future(), loop.create_future()
            for profile, future in [('ar', first), ('en', second)]:
                c.queue.put_nowait(Job(Image.new('RGB', (20, 20)), profile, future, time.monotonic()))
            task = asyncio.create_task(c.consume())
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            first.cancel()
            await asyncio.sleep(.05)
            self.assertEqual(calls, ['ar'])
            release.set()
            self.assertEqual((await second)['profile'], 'en')
            await c.queue.join()
            self.assertEqual(calls, ['ar', 'en'])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            c.pool.shutdown()
        asyncio.run(check())

    def test_gpu_failure_stops_queued_inference(self):
        async def check():
            calls = []
            class Broken(Runtime):
                def infer(self, image, profile):
                    calls.append(profile)
                    raise RuntimeError('simulated GPU error')
            c = Controller(Broken)
            await c.initialize()
            loop = asyncio.get_running_loop()
            futures = [loop.create_future(), loop.create_future()]
            for future in futures:
                c.queue.put_nowait(Job(Image.new('RGB', (20, 20)), 'en', future, time.monotonic()))
            with self.assertLogs('plate_ocr', level='ERROR'):
                task = asyncio.create_task(c.consume())
                for future in futures:
                    with self.assertRaises(HTTPException) as failure:
                        await future
                    self.assertEqual(failure.exception.status_code, 503)
            await c.queue.join()
            self.assertFalse(c.ready)
            self.assertEqual(calls, ['en'])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            c.pool.shutdown()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main()
