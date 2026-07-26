"""
Circuit Breaker Pattern
==========================
"""

import os
import sys
import asyncio
import logging
import time
from typing import Dict, Optional, Any, Set, List, Tuple
from collections import defaultdict, deque
from enum import Enum

# Add parent directory to path
parent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from config import settings
from backend.config import *
from backend.core.metrics import *
logger = logging.getLogger(__name__)

# Circuit Breaker Pattern
# =====================================================
class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker for database operations"""

    def __init__(self, failure_threshold: int = 5, timeout: int = 60, half_open_timeout: int = None):
        self.failure_threshold = failure_threshold
        self.half_open_timeout = half_open_timeout
        self.timeout = timeout
        self.failures = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        self._lock = asyncio.Lock()

    async def call_succeeded(self):
        """Record successful call"""
        async with self._lock:
            self.failures = 0
            self.state = CircuitState.CLOSED

    async def call_failed(self):
        """Record failed call"""
        async with self._lock:
            self.failures += 1
            self.last_failure_time = time.time()

            if self.failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.warning(f"Circuit breaker opened after {self.failures} failures")

    async def can_execute(self) -> bool:
        """Check if operation can be executed"""
        async with self._lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                if time.time() - self.last_failure_time >= self.timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("Circuit breaker half-open, attempting recovery")
                    return True
                return False

            # HALF_OPEN state
            return True

    async def get_status(self) -> dict:
        """Get circuit breaker status"""
        async with self._lock:
            return {
                "state": self.state.value,
                "failures": self.failures,
                "threshold": self.failure_threshold,
                "timeout": self.timeout,
                "last_failure_time": self.last_failure_time,
                "time_since_last_failure": time.time() - self.last_failure_time if self.last_failure_time else None
            }


db_circuit_breaker = CircuitBreaker()


db_circuit_breaker = CircuitBreaker()
