"""
Shared test infrastructure.

asyncpg connections are LOOP-BOUND: the pooled engine binds to the event
loop that first initialized it. If every test module created its own loop,
the second module's helpers would fail with "attached to a different loop".
All async test helpers must therefore run on THIS single shared loop.
"""

import asyncio

SHARED_LOOP = asyncio.new_event_loop()


def run_on_shared_loop(coro):
    return SHARED_LOOP.run_until_complete(coro)
