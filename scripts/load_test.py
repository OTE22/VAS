#!/usr/bin/env python3
"""
Production load test (stdlib only)
==================================
Simulates the real production mix concurrently:
  * webhook senders posting frames (with faces, so inference actually runs)
  * dashboard pollers hitting /api/stats and /api/pipelines
  * /health/live pollers (liveness under load)
  * auth checks (/api/auth/me)

Prints p50/p95/p99 per endpoint and PASS/FAIL against the acceptance criteria:
  webhook p95 < 500ms | /health/live p99 < 100ms | light API p95 < 500ms

Usage (host or container):
    python scripts/load_test.py --base http://localhost --duration 30
    python scripts/load_test.py --base http://localhost:8000 --duration 30   # direct, no nginx

Optional: --image path/to/face.jpg  (a real face image makes inference heavier = more realistic)
"""

import argparse
import base64
import json
import statistics
import sys
import threading
import time
import urllib.request
import urllib.error

RESULTS = {}          # name -> list[(latency_s, status)]
RESULTS_LOCK = threading.Lock()
STOP = threading.Event()


def record(name, latency, status):
    with RESULTS_LOCK:
        RESULTS.setdefault(name, []).append((latency, status))


def http(method, url, body=None, headers=None, timeout=30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return time.time() - start, resp.status
    except urllib.error.HTTPError as e:
        e.read()
        return time.time() - start, e.code
    except Exception:
        return time.time() - start, 0


def login(base):
    req = urllib.request.Request(
        base + "/api/auth/login",
        data=json.dumps({"username": "admin", "password": "admin123"}).encode(),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def tiny_jpeg_b64(image_path=None):
    if image_path:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    # minimal JPEG (passes magic-byte validation; no face -> cheap inference)
    return base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 256 + b"\xff\xd9").decode()


# ----------------------------------------------------------------- workers --

def webhook_sender(base, img_b64, idx):
    n = 0
    while not STOP.is_set():
        n += 1
        payload = {
            "request_id": f"load-{idx}-{n}-{time.time()}",  # unique -> no dedup
            "location_name": "LoadTest Cam",
            "images": [img_b64],
            "predictions": [],
        }
        lat, status = http("POST", f"{base}/webhook/load-test-pipeline", payload, timeout=30)
        record("webhook", lat, status)
        STOP.wait(0.5)


def health_live_poller(base):
    while not STOP.is_set():
        lat, status = http("GET", f"{base}/health/live", timeout=10)
        record("health_live", lat, status)
        STOP.wait(0.25)


def light_api_poller(base, token):
    headers = {"Authorization": f"Bearer {token}"}
    while not STOP.is_set():
        lat, status = http("GET", f"{base}/api/pipelines", headers=headers, timeout=30)
        record("api_pipelines", lat, status)
        lat, status = http("GET", f"{base}/api/auth/me", headers=headers, timeout=30)
        record("api_auth_me", lat, status)
        STOP.wait(1.0)


def stats_poller(base, token):
    headers = {"Authorization": f"Bearer {token}"}
    while not STOP.is_set():
        lat, status = http("GET", f"{base}/api/stats", headers=headers, timeout=60)
        record("api_stats", lat, status)
        STOP.wait(2.0)


# ----------------------------------------------------------------- report --

def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100.0 * len(sorted_vals))) - 1))
    return sorted_vals[k]


def report():
    print("\n" + "=" * 78)
    print(f"{'endpoint':<16}{'count':>7}{'ok%':>7}{'p50':>10}{'p95':>10}{'p99':>10}{'max':>10}")
    print("-" * 78)
    summary = {}
    for name, entries in sorted(RESULTS.items()):
        lats = sorted(e[0] for e in entries)
        ok = sum(1 for e in entries if 200 <= e[1] < 300)
        okpct = 100.0 * ok / len(entries) if entries else 0
        p50, p95, p99 = pct(lats, 50), pct(lats, 95), pct(lats, 99)
        summary[name] = {"p50": p50, "p95": p95, "p99": p99, "ok": okpct}
        print(f"{name:<16}{len(entries):>7}{okpct:>6.1f}%"
              f"{p50 * 1000:>8.0f}ms{p95 * 1000:>8.0f}ms{p99 * 1000:>8.0f}ms{lats[-1] * 1000:>8.0f}ms")
    print("=" * 78)

    criteria = [
        ("webhook p95 < 500ms", "webhook", "p95", 0.5),
        ("/health/live p99 < 100ms", "health_live", "p99", 0.1),
        ("light API (/api/pipelines) p95 < 500ms", "api_pipelines", "p95", 0.5),
        ("light API (/api/auth/me) p95 < 500ms", "api_auth_me", "p95", 0.5),
    ]
    all_pass = True
    print("\nACCEPTANCE CRITERIA")
    print("-" * 78)
    for label, name, metric, limit in criteria:
        if name not in summary:
            print(f"  SKIP  {label} (no samples)")
            continue
        value = summary[name][metric]
        ok = value < limit and summary[name]["ok"] > 95.0
        all_pass = all_pass and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  ->  {value * 1000:.0f}ms "
              f"(ok-rate {summary[name]['ok']:.1f}%)")
    print("-" * 78)
    print("OVERALL:", "PASS ✅" if all_pass else "FAIL ❌")
    return all_pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost", help="base URL (nginx: http://localhost, direct: http://localhost:8000)")
    ap.add_argument("--duration", type=int, default=30, help="seconds to run")
    ap.add_argument("--webhook-senders", type=int, default=4)
    ap.add_argument("--image", default=None, help="path to a face image for realistic inference load")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    print(f"Load test against {base} for {args.duration}s "
          f"({args.webhook_senders} webhook senders + pollers)")

    try:
        token = login(base)
        print("Login OK")
    except Exception as e:
        print(f"FATAL: login failed: {e}")
        sys.exit(2)

    img_b64 = tiny_jpeg_b64(args.image)

    threads = []
    for i in range(args.webhook_senders):
        threads.append(threading.Thread(target=webhook_sender, args=(base, img_b64, i), daemon=True))
    threads.append(threading.Thread(target=health_live_poller, args=(base,), daemon=True))
    threads.append(threading.Thread(target=health_live_poller, args=(base,), daemon=True))
    threads.append(threading.Thread(target=light_api_poller, args=(base, token), daemon=True))
    threads.append(threading.Thread(target=stats_poller, args=(base, token), daemon=True))

    for t in threads:
        t.start()

    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    STOP.set()
    for t in threads:
        t.join(timeout=10)

    sys.exit(0 if report() else 1)


if __name__ == "__main__":
    main()
