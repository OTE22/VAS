"""
Backend Routes
==============
All API route definitions.
"""

from backend.routes.webhook import router as webhook_router
from backend.routes.detections import router as detections_router
from backend.routes.stats import router as stats_router
from backend.routes.upload import router as upload_router
from backend.routes.enrollment_review import router as enrollment_review_router
from backend.routes.health import router as health_router
from backend.routes.metrics import router as metrics_router
from backend.routes.websocket import router as websocket_router
from backend.routes.dashboard import router as dashboard_router
from backend.routes.cache import router as cache_router
from backend.routes.management import router as management_router
from backend.routes.auth import router as auth_router
from backend.routes.users import router as users_router
from backend.routes.audit import router as audit_router

try:
    from backend.routes.identities import router as identities_router
except ImportError:
    identities_router = None

try:
    from backend.routes.admin_tutorial import router as admin_tutorial_router
except ImportError:
    admin_tutorial_router = None

try:
    from backend.routes.settings import router as settings_router
except ImportError:
    settings_router = None

try:
    from backend.routes.logs import router as logs_router
except ImportError:
    logs_router = None

__all__ = [
    'webhook_router',
    'detections_router',
    'stats_router',
    'upload_router',
    'enrollment_review_router',
    'health_router',
    'metrics_router',
    'websocket_router',
    'dashboard_router',
    'cache_router',
    'management_router',
    'auth_router',
    'users_router',
    'audit_router',
    'identities_router',
    'admin_tutorial_router',
    'settings_router',
    'logs_router',
]
