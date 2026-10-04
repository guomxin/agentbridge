"""Serve the actual ES module graph through both authenticated applications."""
from contextlib import contextmanager
import re
import shutil
import threading
from urllib.request import urlopen
from urllib.error import HTTPError

import pytest

from bscli.admin.application import AdminControlPlane
from bscli.admin.server import create_admin_http_server, validate_admin_server_config
from bscli.core.central_service import CentralCapabilityService
from bscli.core.mcp_identities import McpIdentityTokenStore
from bscli.workspace.application import WorkspaceApplication
from bscli.workspace.server import create_workspace_http_server, validate_workspace_server_config
import bscli.workspace.server as workspace_server
from tests.test_workspace import _free_port


@contextmanager
def frontend(home, domain):
    service = CentralCapabilityService(home=home, base_url='http://oa.test')
    port = _free_port()
    origin = f'http://127.0.0.1:{port}'
    config_args = dict(host='127.0.0.1', port=port, public_base_url=origin, tls_cert=None, tls_key=None)
    if domain == 'workspace':
        server = create_workspace_http_server(config=validate_workspace_server_config(**config_args),
            application=WorkspaceApplication(service=service, gateway=None))
    else:
        server = create_admin_http_server(config=validate_admin_server_config(**config_args),
            control_plane=AdminControlPlane(service=service, identity_store=McpIdentityTokenStore(service.db_path)))
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield origin
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize('domain', ['workspace', 'admin'])
def test_real_module_graph_is_served_with_javascript_mime_and_no_stale_module_cache(tmp_path, domain):
    with frontend(tmp_path, domain) as origin:
        with urlopen(origin, timeout=5) as response:
            page = response.read().decode()
            assert response.headers['Cache-Control'] == 'no-store'
            assert "script-src 'self'" in response.headers['Content-Security-Policy']
        entry = re.search(r'<script[^>]*type="module"[^>]*src="([^"]+)"', page)
        assert entry, 'entry must be a browser module'
        pending = [entry.group(1)]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            with urlopen(origin + path, timeout=5) as response:
                assert response.headers['Content-Type'].split(';')[0] in ('text/javascript', 'application/javascript')
                if path.endswith('.mjs'):
                    assert response.headers['Cache-Control'] == 'no-store'
                source = response.read().decode()
            for relative in re.findall(r'\bfrom\s+[\'"](\./[^\'"]+\.mjs)[\'"]', source):
                pending.append('/assets/' + relative[2:])
        assert len(visited) >= (9 if domain == 'workspace' else 5)
        with pytest.raises(HTTPError) as error:
            urlopen(origin + '/assets/../server.py', timeout=5)
        assert error.value.code == 404


def test_workspace_version_includes_imported_modules(tmp_path, monkeypatch):
    root = tmp_path / 'static'
    shutil.copytree(workspace_server.STATIC_ROOT, root)
    monkeypatch.setattr(workspace_server, 'STATIC_ROOT', root)
    before = workspace_server._workspace_asset_version()
    module = next(root.glob('*.mjs'))
    module.write_bytes(module.read_bytes() + b'\n// changed imported module\n')
    assert workspace_server._workspace_asset_version() != before
