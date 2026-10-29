"""Check every inference-model file against the recorded model manifest."""
import hashlib
import json
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    models = json.loads((root / 'models-lock.json').read_text())
    for model in models:
        for filename, expected in model['files'].items():
            file = root / 'models' / model['model'] / filename
            actual = hashlib.sha256(file.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f'Model checksum mismatch: {model["model"]}/{filename}')
    print(f'Verified {len(models)} offline models')
