"""
100k-scale benchmark for the exact (flat) index.
================================================
EXCLUDED from normal CI; MANDATORY for the release-readiness verdict.

    docker exec face_recognition_api python -m pytest -m benchmark -v -s

The agreed budget, which the verdict must cite with measured numbers rather
than assume:

    p95 search   <= 50 ms at 100k vectors
    throughput   >= 20 searches/s sustained
    resident     <= 512 MB for the index

`IndexFlatIP` stays unless these are violated. At 100k, 512-dim float32 is
~205 MB of vectors and an exact scan is memory-bandwidth bound — IVF/HNSW would
add recall loss and training complexity for no measured benefit. If a future
scale breaks the budget, this file is the evidence that justifies swapping.

Every run prints hardware, FAISS thread settings, p50/p95/p99, throughput and
resident memory, so a verdict can quote real figures.
"""

import os
import time

import numpy as np
import pytest

from backend.core.vector_index import EMBEDDING_DIM, FlatFaissIndex

DIM = EMBEDDING_DIM
N_VECTORS = 100_000
N_QUERIES = 200

# Agreed budget
MAX_P95_MS = 50.0
MIN_THROUGHPUT_QPS = 20.0
MAX_RESIDENT_MB = 512.0

pytestmark = pytest.mark.benchmark


def _rss_mb() -> float:
    """Resident set size without psutil (which is not installed here)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) / 1024.0
    except OSError:
        pass
    return float("nan")


def _hardware() -> dict:
    import faiss

    cpus = os.cpu_count()
    mem_total = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    mem_total = float(line.split()[1]) / 1024.0 / 1024.0
                    break
    except OSError:
        pass
    return {
        "cpu_count": cpus,
        "mem_total_gb": round(mem_total, 2) if mem_total else None,
        "faiss_version": getattr(faiss, "__version__", "unknown"),
        "faiss_threads": faiss.omp_get_max_threads(),
        "faiss_gpus": faiss.get_num_gpus() if hasattr(faiss, "get_num_gpus") else 0,
    }


def test_flat_index_meets_the_100k_budget(tmp_path, capsys):
    hardware = _hardware()
    rng = np.random.default_rng(20260802)

    baseline_mb = _rss_mb()
    index = FlatFaissIndex(str(tmp_path / "bench"), model_version="bench")

    # Build in batches so peak memory reflects steady state, not a 200 MB temp.
    build_start = time.perf_counter()
    batch = 10_000
    for start in range(0, N_VECTORS, batch):
        block = rng.standard_normal((batch, DIM)).astype(np.float32)
        block /= np.linalg.norm(block, axis=1, keepdims=True)
        index.add_many([(start + i, block[i]) for i in range(batch)])
    build_seconds = time.perf_counter() - build_start
    resident_mb = _rss_mb() - baseline_mb

    assert index.stats()["count"] == N_VECTORS

    queries = rng.standard_normal((N_QUERIES, DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)

    for i in range(20):                       # warm up caches
        index.search(queries[i % N_QUERIES], top_k=5)

    latencies = []
    search_start = time.perf_counter()
    for i in range(N_QUERIES):
        t0 = time.perf_counter()
        index.search(queries[i], top_k=5, threshold=0.0)
        latencies.append((time.perf_counter() - t0) * 1000.0)
    wall = time.perf_counter() - search_start

    latencies.sort()
    p50 = latencies[int(0.50 * len(latencies))]
    p95 = latencies[int(0.95 * len(latencies))]
    p99 = latencies[int(0.99 * len(latencies))]
    throughput = N_QUERIES / wall

    save_start = time.perf_counter()
    index.save()
    save_seconds = time.perf_counter() - save_start
    snapshot_mb = sum(
        os.path.getsize(os.path.join(root, f))
        for root, _d, files in os.walk(str(tmp_path / "bench")) for f in files
    ) / 1024.0 / 1024.0

    report = {
        "vectors": N_VECTORS,
        "dim": DIM,
        "queries": N_QUERIES,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "p99_ms": round(p99, 3),
        "throughput_qps": round(throughput, 1),
        "resident_mb": round(resident_mb, 1),
        "theoretical_vector_mb": round(N_VECTORS * DIM * 4 / 1024 / 1024, 1),
        "build_seconds": round(build_seconds, 1),
        "save_seconds": round(save_seconds, 2),
        "snapshot_mb": round(snapshot_mb, 1),
        "budget": {"max_p95_ms": MAX_P95_MS,
                   "min_throughput_qps": MIN_THROUGHPUT_QPS,
                   "max_resident_mb": MAX_RESIDENT_MB},
        "hardware": hardware,
    }

    with capsys.disabled():
        print("\n" + "=" * 68)
        print("FLAT INDEX BENCHMARK @ 100k — measured, not assumed")
        print("=" * 68)
        for key, value in report.items():
            print(f"  {key:26} {value}")
        print("=" * 68)

    assert p95 <= MAX_P95_MS, (
        f"p95 {p95:.1f} ms exceeds the {MAX_P95_MS} ms budget — flat exact "
        f"search is no longer adequate at this scale; this is the evidence "
        f"required to justify swapping to HNSW/IVF")
    assert throughput >= MIN_THROUGHPUT_QPS, (
        f"throughput {throughput:.1f} qps below the {MIN_THROUGHPUT_QPS} qps budget")
    assert resident_mb <= MAX_RESIDENT_MB, (
        f"resident {resident_mb:.1f} MB exceeds the {MAX_RESIDENT_MB} MB budget")


def test_rebuild_at_scale_is_bounded(tmp_path, capsys):
    """Recovery must stay practical: a full rebuild is the recovery path, so its
    cost is an operational number worth recording."""
    rng = np.random.default_rng(7)
    index = FlatFaissIndex(str(tmp_path / "bench2"), model_version="bench")

    block = rng.standard_normal((N_VECTORS, DIM)).astype(np.float32)
    block /= np.linalg.norm(block, axis=1, keepdims=True)

    start = time.perf_counter()
    index.add_many([(i, block[i]) for i in range(N_VECTORS)])
    seconds = time.perf_counter() - start

    with capsys.disabled():
        print(f"\n  in-memory index build of {N_VECTORS} vectors: {seconds:.1f}s "
              f"({N_VECTORS / seconds:,.0f} vectors/s)")
    assert index.stats()["count"] == N_VECTORS
