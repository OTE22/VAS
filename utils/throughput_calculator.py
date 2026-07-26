"""
Throughput Calculator
====================
Calculates if the system can handle the expected load.
"""

def calculate_throughput_capacity():
    """
    Calculate system throughput capacity for 50+ cameras.
    
    Assumptions:
    - 50 cameras
    - Each camera sends 1-2 FPS (frames per second)
    - Average 2-3 faces per image
    - GPU processing: ~100-200ms per image
    - CPU processing: ~300-500ms per image
    """
    
    # Load requirements
    cameras = 50
    fps_per_camera = 2  # Worst case: 2 FPS
    total_requests_per_second = cameras * fps_per_camera  # 100 req/s
    
    # Current GPU configuration (updated)
    queue_workers = 50  # Increased for guaranteed support
    max_concurrent = 500  # Increased for high concurrency
    queue_size = 10000  # Increased buffer (10s at 100 req/s)
    batch_size = 20
    pipeline_batch_size = 5  # Batch images from same pipeline
    
    # Processing times (with GPU)
    avg_processing_time_ms = 150  # 100-200ms average
    avg_processing_time_s = avg_processing_time_ms / 1000  # 0.15s
    
    # Throughput capacity
    # Each worker can process: 1 / avg_processing_time_s images per second
    images_per_worker_per_second = 1 / avg_processing_time_s  # ~6.67 images/s
    total_capacity = queue_workers * images_per_worker_per_second  # ~200 images/s
    
    # Safety margin (use 70% of capacity)
    safe_capacity = total_capacity * 0.7  # ~140 images/s
    
    # Queue buffer time
    # How long can queue buffer at max load?
    queue_buffer_seconds = queue_size / total_requests_per_second  # 50 seconds
    
    return {
        "load_requirement": {
            "cameras": cameras,
            "fps_per_camera": fps_per_camera,
            "total_requests_per_second": total_requests_per_second,
        },
        "capacity": {
            "queue_workers": queue_workers,
            "max_concurrent": max_concurrent,
            "images_per_worker_per_second": round(images_per_worker_per_second, 2),
            "total_capacity_per_second": round(total_capacity, 2),
            "safe_capacity_per_second": round(safe_capacity, 2),
        },
        "analysis": {
            "can_handle_load": safe_capacity >= total_requests_per_second,
            "headroom": round(safe_capacity - total_requests_per_second, 2),
            "headroom_percent": round(((safe_capacity - total_requests_per_second) / total_requests_per_second) * 100, 1),
            "queue_buffer_seconds": round(queue_buffer_seconds, 1),
        },
        "recommendations": []
    }


if __name__ == "__main__":
    result = calculate_throughput_capacity()
    
    print("\n" + "="*70)
    print("THROUGHPUT ANALYSIS FOR 50+ CAMERAS")
    print("="*70)
    print(f"\n📊 Load Requirement:")
    print(f"   Cameras: {result['load_requirement']['cameras']}")
    print(f"   FPS per camera: {result['load_requirement']['fps_per_camera']}")
    print(f"   Total requests/second: {result['load_requirement']['total_requests_per_second']}")
    
    print(f"\n⚙️  System Capacity:")
    print(f"   Queue workers: {result['capacity']['queue_workers']}")
    print(f"   Images/worker/second: {result['capacity']['images_per_worker_per_second']}")
    print(f"   Total capacity: {result['capacity']['total_capacity_per_second']} images/s")
    print(f"   Safe capacity (70%): {result['capacity']['safe_capacity_per_second']} images/s")
    
    print(f"\n✅ Analysis:")
    can_handle = result['analysis']['can_handle_load']
    status = "✅ YES" if can_handle else "❌ NO"
    print(f"   Can handle load: {status}")
    print(f"   Headroom: {result['analysis']['headroom']} images/s ({result['analysis']['headroom_percent']}%)")
    print(f"   Queue buffer: {result['analysis']['queue_buffer_seconds']} seconds")
    
    if not can_handle:
        print(f"\n⚠️  RECOMMENDATIONS:")
        print(f"   - Increase QUEUE_WORKERS to {int(result['load_requirement']['total_requests_per_second'] / result['capacity']['images_per_worker_per_second'] * 1.5)}")
        print(f"   - Increase MAX_QUEUE_SIZE to {int(result['load_requirement']['total_requests_per_second'] * 10)}")
        print(f"   - Consider reducing FPS per camera or adding more GPU instances")
    
    print("="*70 + "\n")

