"""Local synthetic Central MCP -> actual OC reply hook/SQLite/Telegram SDK proof."""
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from uuid import uuid4

import uvicorn
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, bounded_mcp_app
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract, OWNER
from tests.test_workspace_query_projection import register
from tests.test_telegram_interaction_closures import login


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True, type=Path)
    parser.add_argument('--sdk-dir', required=True, type=Path)
    parser.add_argument('--standalone', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='oc-native-card-mcp-') as temporary, ExitStack() as stack:
        home = Path(temporary)
        fixture = LatencyAcceptanceFixture(home/'central', seed=False,
            latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
        stack.callback(fixture.close)
        service = fixture.service
        identities = McpIdentityTokenStore(service.db_path)
        identity = identities.issue(user_subject=OWNER, expected_principal_ref=OWNER, label='isolated-native-card')
        register(service, OWNER, 'openclaw', 'openclaw-gateway', identity['token_id'])
        endpoint, _ = service.tasks.ensure_endpoint(user_subject=OWNER, token_id=identity['token_id'], agent_host='openclaw',
            endpoint_key='telegram:default:1001', client_type='telegram', external_subject='1001', account_id='default',
            conversation_ref='agent:main:telegram:direct:1001', route={'channel':'telegram','to':'1001','accountId':'default'},
            capabilities=['timeline_message','trusted_interaction'])
        fixture.trusted_card_origin = 'https://cards.example.test'
        if args.standalone:
            task = None
            session = service.sessions.find(user_subject=OWNER, system_id='oa')
            service.sessions.mark_expired(session['session_id'])
            card = service.start_login(user_subject=OWNER, expected_principal_ref=OWNER, system_id='oa',
                card_base_url=fixture.trusted_card_origin)['interaction']
            record = service.interactions.get(card['interactionId'], user_subject=OWNER)
        else:
            task = service.tasks.ensure_task(user_subject=OWNER, agent_host='openclaw', host_task_key=str(uuid4()), title='Synthetic native card',
                origin_endpoint_id=endpoint['endpoint_id'], active_conversation_ref=endpoint['conversation_ref'])[0]['task_id']
            record, card = login(fixture, task)
        # No original UI/browser/real account. The controlled fixture completes
        # only this synthetic challenge by its actual CSRF/claim/complete API.
        class Control(BaseHTTPRequestHandler):
            completed = False
            def log_message(self, *_): pass
            def do_POST(self):
                if self.path != '/complete' or Control.completed:
                    self.send_error(409); return
                challenge = service._credential_challenge_id(record)
                csrf = service.challenges.issue_csrf(challenge)
                service.challenges.claim(challenge, csrf_token=csrf, csrf_cookie=csrf)
                service.challenges.complete(challenge, result={'principal':OWNER})
                Control.completed = True
                self.send_response(204); self.end_headers()
        control = ThreadingHTTPServer(('127.0.0.1',0), Control)
        worker = threading.Thread(target=control.serve_forever, daemon=True); worker.start()
        stack.callback(control.server_close); stack.callback(control.shutdown)
        listener = stack.enter_context(socket.socket()); listener.bind(('127.0.0.1',0))
        port = listener.getsockname()[1]; base = f'http://127.0.0.1:{port}'
        mcp = create_central_mcp_server(service=service, identity_store=identities,
            config=validate_central_mcp_server_config(host='127.0.0.1',port=port,public_base_url=base,tls_cert=None,tls_key=None),
            auth_card_base_url=fixture.trusted_card_origin)
        daemon = uvicorn.Server(uvicorn.Config(bounded_mcp_app(mcp, identities),http=BoundedMcpProtocol,ws='none',access_log=False,log_level='error'))
        thread = threading.Thread(target=daemon.run,kwargs={'sockets':[listener]},daemon=True); thread.start()
        stack.callback(thread.join,5); stack.callback(setattr,daemon,'should_exit',True)
        deadline = time.monotonic()+15
        while not daemon.started:
            if not thread.is_alive() or time.monotonic()>deadline: raise RuntimeError('ISOLATED_MCP_START_FAILED')
            time.sleep(.02)
        spec = {'home':str(home/'host'),'sdkDir':str(args.sdk_dir.resolve()),'mcpUrl':base+'/mcp','token':identity['token'],
            'controlUrl':f'http://127.0.0.1:{control.server_port}/complete','taskId':task,'interaction':card,
            'endpoint':{'endpointId':endpoint['endpoint_id'],'userSubject':OWNER,'agentHost':'openclaw','state':'active',
                'clientType':'telegram','externalSubject':'1001','accountId':'default','conversationRef':endpoint['conversation_ref'],
                'route':{'channel':'telegram','to':'1001','accountId':'default'}}}
        # Input and child diagnostics stay private; no card URL/token is echoed.
        result = subprocess.run([str(args.node),str(Path(__file__).with_suffix('.mjs'))],input=json.dumps(spec),text=True,capture_output=True,timeout=90)
        if result.returncode:
            import re
            diagnostic = re.sub(r'https?://\S+', '[url]', result.stderr).replace(identity['token'], '[token]')
            raise RuntimeError('ISOLATED_NATIVE_MCP_PROBE_FAILED: '+diagnostic[:1600])
        evidence = json.loads(result.stdout.splitlines()[-1]); assert evidence['passed'] and Control.completed
        if task: assert service.tasks.get_task(task,user_subject=OWNER)['status']=='waiting_user'
        evidence['observerDidNotAdvanceTask'] = True
        evidence['standaloneCredential'] = args.standalone
        print(json.dumps(evidence))


if __name__ == '__main__': main()
