"""Cache Paddle's pinned NVIDIA wheels using verified, resumable downloads."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / 'wheels'
PARTS = Path('/tmp/vas-ocr-cuda-parts')
CHUNK = 8 * 1024 * 1024


def metadata(spec):
    name, version = spec
    with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=60) as response:
        data = json.load(response)
    candidates = [f for f in data['urls'] if f['filename'].endswith('.whl')
                  and 'x86_64' in f['filename'] and 'manylinux' in f['filename']
                  and ('-py3-' in f['filename'] or '-cp311-' in f['filename'])]
    assert len(candidates) == 1, name
    f = candidates[0]
    return dict(filename=f['filename'], url=f['url'], size=f['size'], sha256=f['digests']['sha256'])


def fetch(item, index):
    folder = PARTS / item['filename']
    folder.mkdir(parents=True, exist_ok=True)
    start, end = index * CHUNK, min((index+1)*CHUNK, item['size'])-1
    target = folder / f'{index:04d}.part'
    if target.exists() and target.stat().st_size == end-start+1:
        return
    for attempt in range(5):
        try:
            req = urllib.request.Request(item['url'], headers={'Range': f'bytes={start}-{end}'})
            with urllib.request.urlopen(req, timeout=60) as response:
                assert response.status == 206
                assert response.headers['Content-Range'] == f'bytes {start}-{end}/{item["size"]}'
                with target.with_suffix('.partial').open('wb') as output:
                    shutil.copyfileobj(response, output)
            assert target.with_suffix('.partial').stat().st_size == end-start+1
            target.with_suffix('.partial').replace(target)
            return
        except Exception:
            if attempt == 4:
                raise
            time.sleep(attempt+1)


if __name__ == '__main__':
    with zipfile.ZipFile(next(CACHE.glob('paddlepaddle_gpu-*.whl'))) as wheel:
        path = next(n for n in wheel.namelist() if n.endswith('.dist-info/METADATA'))
        specs = re.findall(r'^Requires-Dist: (nvidia-[\w-]+)==([\d.]+)', wheel.read(path).decode(), re.M)
    with ThreadPoolExecutor(max_workers=8) as pool:
        manifest = sorted(pool.map(metadata, specs), key=lambda f: f['filename'])
    print(f'CUDA dependencies: {len(manifest)} wheels, {sum(f["size"] for f in manifest)/1e9:.2f} GB', flush=True)
    jobs = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        for item in manifest:
            if not (CACHE / item['filename']).exists():
                jobs.extend(pool.submit(fetch, item, i) for i in range((item['size']+CHUNK-1)//CHUNK))
        for count, future in enumerate(as_completed(jobs), 1):
            future.result()
            if count % 24 == 0 or count == len(jobs):
                print(f'CUDA chunks: {count}/{len(jobs)}', flush=True)
    for item in manifest:
        target = CACHE / item['filename']
        if not target.exists():
            with target.with_suffix('.partial').open('wb') as output:
                for i in range((item['size']+CHUNK-1)//CHUNK):
                    with (PARTS / item['filename'] / f'{i:04d}.part').open('rb') as source:
                        shutil.copyfileobj(source, output)
            target.with_suffix('.partial').replace(target)
        with target.open('rb') as source:
            assert hashlib.file_digest(source, 'sha256').hexdigest() == item['sha256'], item['filename']
        print(f'Verified {item["filename"]}', flush=True)
    lock = ROOT / 'cuda-lock.json'
    if lock.exists():
        assert json.loads(lock.read_text()) == manifest
    else:
        lock.write_text(json.dumps(manifest, indent=2)+'\n')
