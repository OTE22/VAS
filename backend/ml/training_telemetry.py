"""Small stage-boundary snapshots; optional resource sampling cannot fail a job."""
import time
from datetime import datetime, timezone


class TrainingTelemetry:
    def __init__(self, dataset_id=None):
        self.dataset_id = dataset_id
        self.events = []
        self.started = time.monotonic()
        self.previous = self.started

    def stage(self, name):
        now = time.monotonic()
        if self.events:
            self.events[-1]["duration_seconds"] = round(now - self.previous, 3)
        event = {"stage": name, "started_at": datetime.now(timezone.utc).isoformat()}
        self.events.append(event)
        self.previous = now
        resources = {"sampled_at": event["started_at"], "scope": "training process at stage boundary"}
        try:
            import psutil
            process = psutil.Process()
            resources["memory_mb"] = round(process.memory_info().rss / 1024 ** 2, 1)
            cpu = process.cpu_times()
            resources["cpu_seconds"] = round(cpu.user + cpu.system, 2)
        except Exception:
            resources["status"] = "unavailable"
        return {"stage": name, "dataset_id": self.dataset_id,
                "stage_history": [dict(e) for e in self.events], "resource_usage": resources}
