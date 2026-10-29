"""Offline synthetic smoke/latency check; not a camera accuracy benchmark."""
from io import BytesIO
import json
import os
import statistics
import time
import urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, features

BASE = 'http://127.0.0.1:8091'


def request(path, payload=None):
    headers = {'Content-Type': 'image/png'} if payload else {}
    if token_file := os.environ.get('VEHICLE_WORKER_TOKEN_FILE'):
        headers['Authorization'] = 'Bearer ' + Path(token_file).read_text().strip()
    req = urllib.request.Request(BASE+path, data=payload,
        headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def sample(label, arabic=False):
    if arabic and not features.check('raqm'):
        # Rendered with RAQM shaping; unshaped Arabic would be an invalid test.
        return (Path(__file__).parent / 'fixtures' / 'arabic.png').read_bytes()
    image = Image.new('RGB', (560, 120), 'white')
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 52)
    options = {'direction': 'rtl', 'language': 'ar'} if arabic and features.check('raqm') else {}
    ImageDraw.Draw(image).text((24, 22), label, fill='black', font=font, **options)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


if __name__ == '__main__':
    print(json.dumps({'health': request('/health/ready')}, ensure_ascii=False), flush=True)
    timings = []
    for profile, text in [('en', 'B 123456'), ('latin', 'AB 123 CD'), ('ar', 'لبنان ١٢٣٤٥')]:
        payload = sample(text, profile == 'ar')
        for trial in range(3):
            start = time.monotonic()
            result = request('/recognize?profile='+profile, payload)
            elapsed = round((time.monotonic()-start)*1000, 2)
            timings.append(elapsed)
            assert result['device'] == 'gpu:0' and result['review_required'] is True
            if trial == 0:
                print(json.dumps({'synthetic_profile': profile, 'expected': text,
                                  'response': result}, ensure_ascii=False), flush=True)
            joined = ''.join(line['text'] for line in result['lines']).replace(' ', '')
            assert joined == text.replace(' ', ''), joined
    print(json.dumps({'synthetic_requests': len(timings),
                      'http_median_ms': statistics.median(timings),
                      'http_max_ms': max(timings),
                      'note': 'Includes intentional worker rate limiting; not field accuracy.'}), flush=True)
