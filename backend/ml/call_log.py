"""
ML-Ops call log — one structured line per /api/ml/* request, for debugging.

Every ML-Ops route is wrapped (APIRoute subclass, see backend/routes/ml_ops.py)
so that each call leaves a JSON record with: request id (the same
X-Request-ID the client receives and the `req=` every other log line of that
request carries), actor, method, route template + concrete path, query, a
SANITISED body summary, status, error code, duration and the job/model/
dataset id the call produced or touched.

Sink: the application log, through the module logger — tagged [MLOPS_CALL]
so it can be grepped on its own (`grep MLOPS_CALL app.log`). utils/logging.py
is the only place that configures handlers; this module attaches none.
GET /api/ml/calls reads the records back from the tail of the active log
file (utils.logging.active_log_path), with an in-process ring buffer as the
fallback when the file is not readable.

Never logged: feature vectors, labels' notes, reasons beyond 200 chars,
anything named like a secret, filesystem paths, or response bodies.
"""

import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from utils.logging import active_log_path, request_id_var

TAG = "[MLOPS_CALL]"
_SECRET_HINTS = ("password", "token", "secret", "authorization", "cookie", "api_key", "apikey")
_ID_KEYS = ("job_id", "task_id", "model_id", "dataset_id", "prediction_id", "label_id")

logger = logging.getLogger("backend.ml.call_log")
_recent: "deque[Dict[str, Any]]" = deque(maxlen=500)   # in-process ring buffer (fallback reader)


_FREE_TEXT_HINTS = ("note", "notes", "comment", "description")


def sanitize_body(body: Any, *, max_str: int = 200) -> Any:
    """Keys + bounded scalars only; secrets and nested bulk are elided."""
    if isinstance(body, dict):
        out = {}
        for key, value in body.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _SECRET_HINTS):
                out[key] = "<redacted>"
            elif any(hint in lowered for hint in _FREE_TEXT_HINTS):
                # analyst notes are free text about a surveilled subject:
                # never in the log line (the contract at the top of this
                # module promises exactly that) - only their length.
                out[key] = f"<text:{len(str(value)) if value is not None else 0}>"
            elif isinstance(value, (dict, list)):
                out[key] = f"<{type(value).__name__}:{len(value)}>"
            elif isinstance(value, str):
                out[key] = value if len(value) <= max_str else value[:max_str] + "…"
            else:
                out[key] = value
        return out
    if isinstance(body, list):
        return f"<list:{len(body)}>"
    return None


def extract_ids(payload: Any) -> Dict[str, Any]:
    """The ids a response produced/touched (top level only)."""
    if not isinstance(payload, dict):
        return {}
    return {k: payload[k] for k in _ID_KEYS if k in payload and isinstance(payload[k], (str, int))}


def record(entry: Dict[str, Any]) -> None:
    """Emit one call record: app log (tagged) + ring buffer."""
    entry = dict(entry)
    entry.setdefault("ts", datetime.utcnow().isoformat() + "Z")
    entry.setdefault("request_id", request_id_var.get())
    line = json.dumps(entry, sort_keys=True, default=str, separators=(",", ":"))
    _recent.appendleft(entry)
    logger.info("%s %s", TAG, line)


def _tail_lines(path: str, max_bytes: int = 2 * 1024 * 1024) -> List[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, max_bytes)
        f.seek(size - chunk)
        return f.read().decode("utf-8", errors="replace").splitlines()


def read_recent(limit: int = 50, *, status_min: Optional[int] = None,
                path_contains: Optional[str] = None) -> List[Dict[str, Any]]:
    """Newest-first records from the active application log (falls back to
    the in-process ring buffer when the file is unavailable)."""
    limit = max(1, min(int(limit), 500))
    records: List[Dict[str, Any]] = []
    try:
        path = active_log_path()
        if path and os.path.exists(path):
            for raw in reversed(_tail_lines(path)):
                marker = raw.find(TAG + " {")
                if marker < 0:
                    continue
                try:
                    records.append(json.loads(raw[marker + len(TAG) + 1:]))
                except ValueError:
                    continue
    except Exception:
        records = []
    if not records:
        records = list(_recent)
    out = []
    for rec in records:
        if status_min is not None and int(rec.get("status") or 0) < status_min:
            continue
        if path_contains and path_contains not in str(rec.get("path", "")):
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


class CallTimer:
    def __init__(self):
        self.started = time.monotonic()

    @property
    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)
