"""Fetch only the selected public PaddleOCR models before building the image."""
import hashlib
import json
import argparse
import shutil
import tarfile
import urllib.request
from pathlib import Path

MODELS = ('PP-OCRv5_mobile_det', 'arabic_PP-OCRv5_mobile_rec',
          'en_PP-OCRv5_mobile_rec', 'latin_PP-OCRv5_mobile_rec')
ROOT = Path('/opt/plate-ocr/models')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT)
    ROOT = parser.parse_args().output
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name in MODELS:
        url = f'https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/{name}_infer.tar'
        archive = ROOT / f'{name}.tar'
        with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tarfile.open(archive) as tar:
            tar.extractall(ROOT, filter='data')
        extracted = ROOT / f'{name}_infer'
        if extracted.is_dir() and not (ROOT / name).exists():
            extracted.rename(ROOT / name)
        assert (ROOT / name / 'inference.yml').is_file(), name
        files = {str(file.relative_to(ROOT / name)): hashlib.sha256(file.read_bytes()).hexdigest()
                 for file in sorted((ROOT / name).rglob('*')) if file.is_file()}
        manifest.append(dict(model=name, source=url, sha256=digest, files=files))
        archive.unlink()
        print(f'Downloaded {name}', flush=True)
    (ROOT / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    lock = Path(__file__).resolve().parent / 'models-lock.json'
    if lock.exists():
        assert json.loads(lock.read_text()) == manifest, 'Models differ from the pinned checksums'
    else:
        lock.write_text(json.dumps(manifest, indent=2)+'\n')
