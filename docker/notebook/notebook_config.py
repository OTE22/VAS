"""Workspace authenticated exclusively through the VAS admin gateway."""
import os
from pathlib import Path

c = get_config()  # noqa: F821 — supplied by Jupyter
# Fail closed rather than accepting an empty token or silently generating one.
token = Path(os.environ['JUPYTER_TOKEN_FILE']).read_text().strip()
if len(token) < 32:
    raise RuntimeError('Notebook token must contain at least 32 characters')
import sys
sys.path.insert(0, '/etc/jupyter')
from gateway_identity import GatewayIdentityProvider
c.ServerApp.identity_provider_class = GatewayIdentityProvider
c.IdentityProvider.token = ''
c.ServerApp.ip = '0.0.0.0'
c.ServerApp.port = 8888
c.ServerApp.base_url = '/notebooks/'
c.ServerApp.trust_xheaders = True
c.ServerApp.port_retries = 0
c.ServerApp.open_browser = False
c.ServerApp.root_dir = '/workspace'
c.ServerApp.allow_remote_access = True
c.ServerApp.allow_origin = ''
c.ServerApp.disable_check_xsrf = False
c.ServerApp.allow_unauthenticated_access = False
c.ServerApp.terminals_enabled = False
# Keep the UI available; release resources by culling idle kernels instead.
c.ServerApp.shutdown_no_activity_timeout = 0
# Keep Jupyter internal signing state outside the temporary runtime directory.
c.ServerApp.cookie_secret_file = '/auth-state/cookie_secret'
c.MappingKernelManager.cull_idle_timeout = 1800
c.MappingKernelManager.cull_connected = False
c.LabApp.user_settings_dir = '/workspace/.jupyter/lab/user-settings'
c.LabApp.workspaces_dir = '/workspace/.jupyter/lab/workspaces'
