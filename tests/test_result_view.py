import http.client
import json
from pathlib import Path
import re
import socket
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from agentbridge.auth.card import MAX_AUTH_BODY_BYTES, TrustedAuthApplication
from agentbridge.auth.result_view import TrustedResultViewApplication, result_view_target
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.core.auth_challenges import AuthChallengeStore


TOKEN = 't' * 43
TASK = '12345678-1234-1234-1234-123456789abc'


class Backend:
    def __init__(self):
        self.calls = []
    def read(self, token):
        self.calls.append(('read', token))
        return {'title': '</script><img src=x onerror=alert(1)>', 'groups': [], 'preparations': []}
    def prepare(self, token, body):
        self.calls.append(('prepare', token, body))
        return {'taskId': TASK, 'status': 'queued'}
    def status(self, token, task_id):
        self.calls.append(('status', token, task_id))
        return {'taskId': task_id, 'state': 'unknown', 'terminal': True}


def session(app, view=TOKEN):
    card = app.get_card(view, secure_cookie=True)
    cookie = card.headers['Set-Cookie'].split(';')[0].split('=', 1)[1]
    data = re.search(r'id="result-data">(.*?)</script>', card.body.decode(), re.S).group(1)
    return card, cookie, json.loads(data)['csrfToken']


class ResultViewTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.app = TrustedResultViewApplication(backend=self.backend)

    def test_page_is_self_contained_escaped_and_cookie_scoped(self):
        card, cookie, proof = session(self.app)
        html = card.body.decode()
        self.assertEqual(card.status, 200)
        self.assertNotIn('</script><img', html)
        self.assertIn('\\u003c/script\\u003e', html)
        self.assertIn('HttpOnly; SameSite=Strict', card.headers['Set-Cookie'])
        self.assertIn('Path=/results/' + TOKEN, card.headers['Set-Cookie'])
        self.assertTrue(card.headers['Set-Cookie'].endswith('; Secure'))
        self.assertIn("connect-src 'self'", card.headers['Content-Security-Policy'])
        self.assertNotIn('src="https://', html)
        self.assertIn('不会授权或提交业务', html)
        self.assertIn('不会取消任务', html)

    def test_csrf_proof_is_bound_to_cookie_and_exact_view(self):
        _, cookie, proof = session(self.app)
        for view, c, p in [(TOKEN, '', proof), (TOKEN, 'x'*43, proof), ('u'*43, cookie, proof),
                            (TOKEN, cookie, '中文'), (TOKEN, cookie, '')]:
            with self.subTest(view=view, cookie=c, proof=p):
                before = len(self.backend.calls)
                response = self.app.prepare(view, cookie=c, token=p,
                    content_type='application/json', body=b'{}')
                self.assertEqual(response.status, 403)
                self.assertEqual(len(self.backend.calls), before)

    def test_prepare_only_delegates_frozen_body_and_status_is_readonly(self):
        _, cookie, proof = session(self.app)
        body = {'operationId': 'op', 'targetKeys': ['a'*64], 'requestKey': 'request-1234567890'}
        r = self.app.prepare(TOKEN, cookie=cookie, token=proof, content_type='application/json',
                             body=json.dumps(body).encode())
        self.assertEqual(r.status, 202)
        self.assertEqual(self.backend.calls[-1], ('prepare', TOKEN, body))
        r = self.app.state(TOKEN, cookie=cookie, token=proof, task_id=TASK)
        self.assertEqual(json.loads(r.body)['state'], 'unknown')
        self.assertEqual(self.backend.calls[-1], ('status', TOKEN, TASK))
        r = self.app.state(TOKEN, cookie=cookie, token=proof, task_id='../foreign')
        self.assertEqual(r.status, 400)
        before = len(self.backend.calls)
        for invalid in ['-' * 36, 'a' * 36, TASK.upper(), TASK + '/other']:
            self.assertEqual(self.app.state(TOKEN, cookie=cookie, token=proof, task_id=invalid).status, 400)
        self.assertEqual(len(self.backend.calls), before)

    def test_invalid_json_and_content_type_do_not_queue(self):
        _, cookie, proof = session(self.app)
        for content_type, body, code in [('text/plain', b'{}', 415), ('application/json', b'not json', 400),
                                         ('application/json', b'x' * (MAX_AUTH_BODY_BYTES + 1), 413)]:
            before = len(self.backend.calls)
            r = self.app.prepare(TOKEN, cookie=cookie, token=proof, content_type=content_type, body=body)
            self.assertEqual(r.status, code)
            self.assertEqual(len(self.backend.calls), before)

    def test_routes_are_closed_and_opaque(self):
        self.assertEqual(result_view_target('/results/'+TOKEN), (TOKEN, 'page'))
        self.assertEqual(result_view_target('/results/'+TOKEN+'/state?task=x'), (TOKEN, 'state'))
        for path in ['/results/short', '/results/'+TOKEN+'/approve', '/results/'+TOKEN+'/../prepare']:
            self.assertIsNone(result_view_target(path))

    def test_http_origin_cookie_and_method_enforcement(self):
        with TemporaryDirectory() as tmp:
            config = validate_auth_server_config(host='127.0.0.1', port=0, public_base_url=None, tls_cert=None, tls_key=None)
            app = TrustedAuthApplication(challenge_store=AuthChallengeStore(Path(tmp)/'db.sqlite'), broker=None)
            server = create_auth_http_server(config=config, application=app, result_application=self.app)
            thread = Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                port = server.server_address[1]
                def request(method, path, body=None, headers=None):
                    c = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                    c.request(method, path, body=body, headers=headers or {})
                    r = c.getresponse(); data = r.read(); h = dict(r.getheaders()); status = r.status; c.close()
                    return status, data, h
                code, data, headers = request('GET', '/results/'+TOKEN)
                self.assertEqual(code, 200)
                cookie = headers['Set-Cookie'].split(';')[0]
                proof = json.loads(re.search(r'id="result-data">(.*?)</script>', data.decode(), re.S).group(1))['csrfToken']
                good = {'Origin': f'http://127.0.0.1:{port}', 'Sec-Fetch-Site': 'same-origin',
                        'Cookie': cookie, 'X-AgentBridge-Result-Token': proof, 'Content-Type': 'application/json'}
                self.assertEqual(request('POST', '/results/'+TOKEN+'/prepare', b'{}', good)[0], 202)
                for patch in [{'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Cookie': ''}]:
                    with self.subTest(patch=patch):
                        before = len(self.backend.calls)
                        self.assertEqual(request('POST', '/results/'+TOKEN+'/prepare', b'{}', {**good, **patch})[0], 403)
                        self.assertEqual(len(self.backend.calls), before)
                for patch in [{'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'},
                              {'Sec-Fetch-Site': 'same-site'}, {'Cookie': ''}, {'X-AgentBridge-Result-Token': '0' * 64}]:
                    with self.subTest(read_patch=patch):
                        before = len(self.backend.calls)
                        self.assertEqual(request('GET', '/results/'+TOKEN+'/state', headers={**good, **patch})[0], 403)
                        self.assertEqual(len(self.backend.calls), before)
                self.assertEqual(request('GET', '/results/'+TOKEN+'/prepare', headers=good)[0], 405)
                self.assertEqual(request('GET', '/results/'+TOKEN+'/state?task=x&task=y', headers=good)[0], 400)
                self.assertEqual(request('GET', '/results/'+TOKEN+'/state?task='+TASK, headers=good)[0], 200)
                before = len(self.backend.calls)
                for method, route in [('GET', '/results/'+TOKEN+'?task='+TASK),
                                      ('GET', '/results/'+TOKEN+'/state?other='+TASK),
                                      ('GET', '/results/'+TOKEN+'/state?task='),
                                      ('POST', '/results/'+TOKEN+'/prepare?task='+TASK)]:
                    self.assertEqual(request(method, route, b'{}' if method=='POST' else None, good)[0], 400)
                self.assertEqual(len(self.backend.calls), before)

                def raw_request(length_headers, body=b'{}'):
                    headers = [f'POST /results/{TOKEN}/prepare HTTP/1.1', f'Host: 127.0.0.1:{port}',
                               *[f'{name}: {value}' for name,value in good.items()], *length_headers]
                    with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
                        sock.sendall(('\r\n'.join(headers)+'\r\n\r\n').encode('ascii')+body)
                        sock.shutdown(socket.SHUT_WR)
                        response = http.client.HTTPResponse(sock); response.begin()
                        response.read()
                        return response.status
                for framing, expected in [([],400), (['Content-Length: 2','Content-Length: 2'],400),
                    (['Content-Length: 2','Transfer-Encoding: chunked'],400),
                    (['Content-Length: +2'],400), (['Content-Length: -1'],400),
                    ([f'Content-Length: {MAX_AUTH_BODY_BYTES+1}'],413), (['Content-Length: 3'],400)]:
                    with self.subTest(framing=framing):
                        before = len(self.backend.calls)
                        self.assertEqual(raw_request(framing), expected)
                        self.assertEqual(len(self.backend.calls), before)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
