"""Only the private gateway may authenticate Jupyter requests.

Keep IdentityProvider.token empty: JupyterLab includes that public property in
its page configuration. The actual service credential is never that property.
"""
import hmac
import os
from pathlib import Path
from jupyter_server.auth import IdentityProvider, User


class GatewayIdentityProvider(IdentityProvider):
    @property
    def auth_enabled(self):
        return True

    @property
    def login_available(self):
        return False

    async def get_user_token(self, handler):
        secret = Path(os.environ['JUPYTER_TOKEN_FILE']).read_text().strip()
        supplied = handler.request.headers.get('Authorization', '')
        if len(secret) >= 32 and hmac.compare_digest(supplied, 'token ' + secret):
            return User(username='vas-admin', name='VAS administrator', display_name='VAS administrator')
        return None

    def get_user_cookie(self, handler):
        return None

    def set_login_cookie(self, handler, user):
        pass
