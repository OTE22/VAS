"""Resumable public CUDA wheel download; run before building this image."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import zipfile

FILENAME = 'paddlepaddle_gpu-3.3.1-cp311-cp311-linux_x86_64.whl'
URL = 'https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/' + FILENAME
SIZE = 2867089443
CHUNK = 8 * 1024 * 1024
ROOT = Path(__file__).resolve().parent
TARGET = ROOT / 'wheels' / FILENAME
PARTS = Path('/tmp/vas-paddle-cu129-download')


def fetch(index):
    start, end = index * CHUNK, min((index+1)*CHUNK, SIZE)-1
    part = PARTS / f'{index:04d}.part'
    if part.exists() and part.stat().st_size == end-start+1:
        return part
    for attempt in range(5):
        try:
            request = urllib.request.Request(URL, headers={'Range': f'bytes={start}-{end}'})
            with urllib.request.urlopen(request, timeout=45) as response:
                expected = f'bytes {start}-{end}/{SIZE}'
                if response.status != 206 or response.headers.get('Content-Range') != expected:
                    raise RuntimeError('Server returned the wrong byte range')
                tmp = part.with_suffix('.partial')
                with tmp.open('wb') as output:
                    remaining = end-start+1
                    while remaining:
                        data = response.read(min(256*1024, remaining))
                        if not data:
                            raise RuntimeError('Truncated runtime download')
                        output.write(data)
                        remaining -= len(data)
            tmp.replace(part)
            return part
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1+attempt)


if __name__ == '__main__':
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    PARTS.mkdir(parents=True, exist_ok=True)
    count = (SIZE+CHUNK-1)//CHUNK
    start = time.monotonic()
    if not TARGET.exists():
        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(fetch, i) for i in range(count)]
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                if done % 16 == 0 or done == count:
                    print(f'Runtime downloaded: {done}/{count} chunks; {time.monotonic()-start:.0f}s', flush=True)
        temp = TARGET.with_suffix('.partial')
        with temp.open('wb') as output:
            for i in range(count):
                with (PARTS / f'{i:04d}.part').open('rb') as source:
                    while data := source.read(1024*1024):
                        output.write(data)
        temp.replace(TARGET)
    assert TARGET.stat().st_size == SIZE
    with zipfile.ZipFile(TARGET) as archive:
        assert archive.testzip() is None, 'Wheel CRC verification failed'
    with TARGET.open('rb') as file:
        digest = hashlib.file_digest(file, 'sha256').hexdigest()
    manifest = {'filename': FILENAME, 'source': URL, 'size': SIZE, 'sha256': digest}
    lock = ROOT / 'runtime-lock.json'
    if lock.exists():
        assert json.loads(lock.read_text()) == manifest, 'Runtime does not match pinned checksum'
    else:
        lock.write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest), flush=True)
