"""Exercise production semaphore nesting without bootstrapping models or DB."""
import ast
import asyncio
from pathlib import Path
import unittest


class FairnessTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_camera_does_not_take_shared_slot(self):
        tree = ast.parse((Path(__file__).parents[1] / 'backend/services/image_processing.py').read_text())
        outer = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncWith)
                     and any(isinstance(c, ast.Name) and c.id == '_inference_semaphore'
                             for c in ast.walk(n))
                     and isinstance(n.body[0], ast.AsyncWith))
        outer.body[0].body = ast.parse('await probe(pipeline_id)').body
        func = ast.parse('async def acquire(pipeline_id):\n    pass').body[0]
        func.body = [outer]
        module = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))
        per = {'busy': asyncio.Semaphore(1), 'other': asyncio.Semaphore(1)}
        shared = asyncio.Semaphore(2)
        busy_started, other_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def probe(key):
            (busy_started if key == 'busy' else other_started).set()
            await release.wait()
        env = {'_inference_semaphore': shared, '_get_pipeline_semaphore': per.__getitem__, 'probe': probe}
        exec(compile(module, '<production semaphore order>', 'exec'), env)
        tasks = [asyncio.create_task(env['acquire']('busy'))]
        try:
            await asyncio.wait_for(busy_started.wait(), 1)
            tasks.append(asyncio.create_task(env['acquire']('busy')))
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(env['acquire']('other')))
            await asyncio.wait_for(other_started.wait(), 1)
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(shared._value, 2)
        self.assertEqual(per['busy']._value, 1)
