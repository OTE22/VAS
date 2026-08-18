"""
Pagination bounds
=================
One place that decides how many rows a listing endpoint may return.

Before this, every listing route carried its own literals — `page_size: int = 20`
here, `Query(default=25, le=100)` there, `limit: int = 100` with no ceiling at
all somewhere else. There was no page-size setting in config.py, so none of it
could be tuned, the numbers drifted apart between backend and frontend (unknown
faces 20 vs 25, search history 50 vs 100, ml-ops 10 vs 25), and four groups of
endpoints accepted any page size a caller cared to send.

Use `resolve_page_size()` in a route that already has its own `page_size`
parameter, or depend on `Pagination` when adding pagination to a new one.

Endpoints with a deliberately different bound (audit export at 1000, watchlist
export at 500) keep their explicit `Query(..., le=N)` — the point is that the
number is stated on purpose, not that every route must be identical.

A note on the frontend: several admin pages still send an explicit `page_size`
(the unknown-faces grid asks for 20, ml-ops for 10). Those are legitimate
presentation choices — how many cards fit a layout is a property of the page,
not a deployment setting — and they are now bounded by API_MAX_PAGE_SIZE like
any other caller. What was wrong before is that the server had no default for
them to fall back to and no ceiling to reject them, so the numbers looked like
configuration while being unreachable and unbounded. A page that wants the
configured default simply omits the parameter.
"""

from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Query

from config import settings


def max_page_size() -> int:
    """Configured ceiling, read per call so an admin edit applies immediately."""
    return int(settings.API_MAX_PAGE_SIZE)


def default_page_size() -> int:
    """Configured default, read per call."""
    return int(settings.API_DEFAULT_PAGE_SIZE)


def resolve_page_size(requested: Optional[int], *, field: str = "page_size") -> int:
    """Validate a caller-supplied page size against the configured bounds.

    `None` means "the caller did not choose", which resolves to the configured
    default. An over-limit request is REFUSED rather than silently clamped: a
    clamp tells the caller nothing, so a UI asking for 500 rows and rendering
    100 looks like missing data rather than a bound it should paginate around.
    """
    ceiling = max_page_size()
    if requested is None:
        return min(default_page_size(), ceiling)
    if requested < 1:
        raise HTTPException(status_code=422, detail=f"{field} must be at least 1")
    if requested > ceiling:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must not exceed the configured maximum of {ceiling}")
    return requested


def resolve_page(requested: Optional[int], *, field: str = "page") -> int:
    """1-based page number."""
    if requested is None:
        return 1
    if requested < 1:
        raise HTTPException(status_code=422, detail=f"{field} must be at least 1")
    return requested


@dataclass
class Pagination:
    """Resolved, validated pagination for one request."""
    page: int
    page_size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def paginate(
    page: Optional[int] = Query(default=None, description="1-based page number"),
    page_size: Optional[int] = Query(
        default=None,
        description="Rows per page (defaults to API_DEFAULT_PAGE_SIZE, "
                    "capped at API_MAX_PAGE_SIZE)"),
) -> Pagination:
    """FastAPI dependency. Bounds come from configuration, never from literals."""
    return Pagination(page=resolve_page(page), page_size=resolve_page_size(page_size))
