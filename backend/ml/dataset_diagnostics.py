"""Bounded dataset-stage evidence; never store source rows or exception locals."""
import logging
import time
import traceback
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)
STAGES = (
    ('configuration', 2), ('counting_source', 10), ('extracting_rows', 20),
    ('matching_labels', 35), ('selecting_features', 45), ('validation', 55),
    ('splitting', 65), ('population_checks', 75), ('writing_artifact', 85),
    ('writing_manifest', 92), ('registering_dataset', 97),
)


class DatasetDiagnostics:
    def __init__(self, job_id=None, publish=None):
        self.job_id = job_id
        self.publish = publish
        self.events = []
        self.started = None
        self.failure = None
        self.dataset_id = None
        self.configuration = {}

    def snapshot(self):
        return {'diagnostics_version': 1, 'stage': self.events[-1]['stage'] if self.events else None,
                'dataset_id': self.dataset_id, 'configuration': dict(self.configuration),
                'stage_history': [dict(e) for e in self.events], 'failure': self.failure}

    async def emit(self):
        if not self.publish:
            return
        try:
            percent = dict(STAGES).get(self.events[-1]['stage'], 0) if self.events else 0
            await self.publish(percent, self.snapshot())
        except Exception:
            # Observability must not change extraction, transactions or outputs.
            logger.warning('Dataset diagnostics could not be persisted for job %s', self.job_id)

    def close(self, status='completed', **counts):
        if self.events and self.events[-1]['status'] == 'running':
            self.events[-1].update(status=status, duration_seconds=round(time.monotonic() - self.started, 3))
            self.events[-1].update(counts)

    async def stage(self, name, **counts):
        self.close(**counts)
        self.started = time.monotonic()
        self.events.append({'stage': name, 'status': 'running',
                            'started_at': datetime.now(timezone.utc).isoformat()})
        await self.emit()

    async def finish(self, result):
        self.dataset_id = result.get('dataset_id')
        failed = result.get('status') == 'failed'
        self.close('failed' if failed else 'completed')
        if failed:
            self.failure = {'code': result.get('refusal') or 'DATASET_QUALITY_FAILED',
                            'failed_checks': (result.get('quality_report') or {}).get('failed_checks', [])}
        await self.emit()

    async def fail(self, exc):
        self.close('failed')
        # Frames identify code locations without exception messages, source text,
        # absolute host paths, database statements, connection strings or locals.
        root = Path(__file__).resolve().parents[2]
        frames = []
        for frame in traceback.extract_tb(exc.__traceback__)[-20:]:
            path = Path(frame.filename).resolve()
            if path.is_relative_to(root / 'backend'):
                frames.append({'module': path.relative_to(root).as_posix(),
                               'line': frame.lineno, 'function': frame.name})
        self.failure = {'code': type(exc).__name__, 'frames': frames,
                        'log_reference': self.job_id}
        await self.emit()


def trace_dataset_build(function):
    @wraps(function)
    async def traced(*args, **kwargs):
        diagnostics = kwargs.pop('diagnostics', None)
        if diagnostics is None:
            job_id = kwargs.get('build_job_id')
            publish = None
            if job_id:
                from backend.core.task_history import task_history_manager
                async def publish(percent, details):
                    await task_history_manager.update_progress(job_id, percent, details=details)
            diagnostics = DatasetDiagnostics(job_id, publish)
        diagnostics.configuration = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in kwargs.items()
            if key in ('name', 'kind', 'time_range_start', 'time_range_end', 'sampling_policy', 'split_strategy')
        }
        definition = kwargs.get('definition')
        if definition is not None:
            diagnostics.configuration['definition'] = definition.to_manifest()
        await diagnostics.stage('configuration')
        try:
            result = await function(*args, diagnostics=diagnostics, **kwargs)
        except Exception as exc:
            await diagnostics.fail(exc)
            raise
        await diagnostics.finish(result)
        result['diagnostics'] = diagnostics.snapshot()
        return result
    return traced
