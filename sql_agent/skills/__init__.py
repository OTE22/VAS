"""Reusable recipes the agent loads only when they apply.

A skill describes HOW to do a category of work - the reasoning procedure, the
tools it may use, and what it must not do. It never contains a specific user
request, and never duplicates the Python that does the work.
"""

from .resolver import compose, resolve

__all__ = ["compose", "resolve"]
