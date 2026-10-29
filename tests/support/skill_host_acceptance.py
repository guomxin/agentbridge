"""Actual OpenClaw agent + candidate plugin, with deny-by-default synthetic OA.

No model runs unless --run or --serve is explicit. --prepare alone is offline.
Each invocation requires an empty private home. --serve starts a separate gateway
and real Workspace for Chrome acceptance; it never installs/restarts a service.
The only imported production material is a short-lived model access token through
the official OpenClaw auth API. Production sessions and business identities stay
outside this fixture. Output is evidence, not an automatic semantic quality claim.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import threading
import time

import uvicorn
from agentbridge.core.central_service import CentralCapabilityService
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import create_central_mcp_server, validate_central_mcp_server_config
from agentbridge.workspace.application import WorkspaceApplication
from agentbridge.workspace.gateway import OpenClawGatewayClient, GatewayRequestError
from agentbridge.workspace.server import create_workspace_http_server, validate_workspace_server_config
from tests.authorization_fixtures import grant_permissions
from tests.support.interaction_acceptance import free_port, write_metadata
from tests.test_workspace import PASSWORD, _create_account

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = 'skill-host-acceptance-v1'
ALICE, BOB = 'skill-host-alice', 'skill-host-bob'
READS = frozenset({'oa.workflow.pending.list', 'oa.workflow.sent.list', 'oa.workflow.done.list'})
TOOLS = ['agentbridge_identity_status', 'agentbridge_skill_catalog', 'agentbridge_skill_get', 'agentbridge_skill_authoring',
         'oa_workflow_pending_list', 'oa_workflow_sent_list', 'oa_workflow_done_list',
         'agentbridge_operation_get', 'agentbridge_operation_list']
CASES_PATH = ROOT / 'tests/fixtures/skill_host_cases.json'


def source_identity():
    """Bounded source manifest plus whole tracked-diff digest, never diff text."""
    paths = set((ROOT / 'agentbridge/core').glob('*skill*.py'))
    paths.update((ROOT / 'integrations/openclaw-agentbridge/lib').rglob('*.js'))
    paths.update((ROOT / 'tests/support').glob('skill_host_acceptance*'))
    for name in ('agentbridge/core/central_service.py', 'agentbridge/core/user_grants.py',
                 'agentbridge/core/host_contract.py', 'agentbridge/mcp/central.py',
                 'agentbridge/workspace/gateway.py', 'agentbridge/workspace/gateway_client.mjs',
                 'integrations/openclaw-agentbridge/index.js', 'integrations/openclaw-agentbridge/package.json',
                 'integrations/openclaw-agentbridge/openclaw.plugin.json', 'tests/fixtures/skill_host_cases.json',
                 'tests/test_skill_host_acceptance.py', 'tests/test_workspace.py',
                 'tests/authorization_fixtures.py', 'tests/support/interaction_acceptance.py'):
        paths.add(ROOT / name)
    files = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(paths) if path.is_file()}
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    diff = subprocess.check_output(['git', 'diff', '--no-ext-diff', '--binary', 'HEAD', '--'], cwd=ROOT)
    return {'git_head': head, 'tracked_diff_sha256': hashlib.sha256(diff).hexdigest(),
            'files_sha256': files, 'relevant_content_sha256': hashlib.sha256(
                json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}


def compare_source_identity(start, end):
    before, after = start['files_sha256'], end['files_sha256']
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    return {'relevant_content_unchanged': not changed, 'changed_files': changed,
            'git_head_unchanged': start['git_head'] == end['git_head'],
            'tracked_diff_unchanged': start['tracked_diff_sha256'] == end['tracked_diff_sha256']}


def private_text(path, text):
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


def run_isolated_process(command, *, env, cwd, timeout):
    """Bound the whole temporary agent process group, including provider children."""
    process = subprocess.Popen(command, env=env, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return process.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try: stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        return 'timeout', stdout, stderr
    finally:
        # The installer can leave npm alive after its own failure/timeout. The
        # entire group was created above, so it never contains the user's host.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(process.pid, sig)
            except ProcessLookupError: break
            if sig == signal.SIGTERM: time.sleep(.1)


def isolated_environment(home, tokens, ca=None):
    # Explicit allowlist excludes inherited business, model and gateway secrets.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR', 'SHELL') if key in os.environ}
    env.update(OPENCLAW_STATE_DIR=str(home / 'openclaw'), OPENCLAW_CONFIG_PATH=str(home / 'openclaw.json'),
               OPENCLAW_NO_RESPAWN='1', SKILL_HOST_ALICE_TOKEN=tokens[ALICE], SKILL_HOST_BOB_TOKEN=tokens[BOB])
    if ca:
        env['NODE_EXTRA_CA_CERTS'] = str(ca)
    return env


def host_config(home, *, mcp_url, gateway_port, gateway_token, provider_plugin, model):
    return {
        'gateway': {'mode': 'local', 'bind': 'loopback', 'port': gateway_port,
                    'auth': {'mode': 'token', 'token': gateway_token},
                    'controlUi': {'enabled': False}},
        'agents': {'defaults': {'workspace': str(home / 'workspace'), 'skipBootstrap': True,
                               'model': {'primary': model, 'fallbacks': []}},
                   'list': [{'id': 'main', 'default': True, 'skills': []}]},
        'auth': {'profiles': {'openai:skill-host-acceptance': {'provider': 'openai', 'mode': 'token'}}},
        'tools': {'allow': TOOLS, 'elevated': {'enabled': False}},
        'browser': {'enabled': False}, 'skills': {'allowBundled': []},
        'plugins': {'allow': ['openai', 'codex', 'agentbridge-interactions'],
                    'slots': {'memory': 'none'},
                    'load': {'paths': [str(provider_plugin), str(ROOT / 'integrations/openclaw-agentbridge')]},
                    'entries': {'openai': {'enabled': True}, 'codex': {'enabled': True},
                        'agentbridge-interactions': {'enabled': True, 'hooks': {'allowConversationAccess': True},
                            'config': {'mcpUrl': mcp_url, 'autoPoll': False, 'syncTimeline': True,
                                       'allowedCardOrigins': ['http://127.0.0.1:1'],
                                       'identityBindings': [
                                           {'channel': 'telegram', 'senderId': ALICE, 'tokenEnv': 'SKILL_HOST_ALICE_TOKEN'},
                                           {'channel': 'telegram', 'senderId': BOB, 'tokenEnv': 'SKILL_HOST_BOB_TOKEN'}]}}}},
        'diagnostics': {'stuckSessionWarnMs': 30000, 'stuckSessionAbortMs': 120000},
    }


class SyntheticWorker:
    def __init__(self, session): self.session = session
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def restore_session_state(self, state): pass
    def capture_session_state(self): return {'cookies': []}


class SyntheticReadService(CentralCapabilityService):
    def invoke(self, *, capability_name, **kwargs):
        if capability_name not in READS:
            raise PermissionError('SYNTHETIC_ONLY: unknown and write capabilities are disabled')
        return super().invoke(capability_name=capability_name, **kwargs)

    def resume_interaction(self, **kwargs):
        raise PermissionError('SYNTHETIC_ONLY: interaction execution is disabled')


def proposals():
    return {
        'weekly': {'name': '周报整理助手', 'description': '把用户提供的原始事项整理为忠实的周报。',
            'selection': {'use_when': '用户要写周报或归纳本周工作', 'not_for': '数学、翻译、闲聊、业务查询或提交', 'output': '完成、风险、计划，保留未完成和不确定状态'},
            'instructions': '只依据用户原文，不补事实、原因、风险推测或后续承诺。先判断有无明确完成项；没有则完成栏只写未提供，不放未完成状态。完成栏仅放明确已完成事项；未开始、未完成放待办或风险并保留原状态。计划栏仅放原文明示安排，保留原时间及不确定性。没有信息的栏目写未提供；缺原文先询问。只有用户要求逐句核对原文时，使用 authoring resource 按本轮 binding_id 读取可选资料 references/weekly-check.md 并依照其中格式输出核对。普通周报无需加载该资料。',
            'profiles': {'use': {}}, 'required_resources': {'use': []},
            'references': {'references/weekly-check.md': '逐句核对格式：增加“原文核对”小节，分别标注每条“原文状态”和“周报归类”。测试还没补必须归入风险/待办并保留未完成状态，不得写已完成；除非原文明示计划，不得改写为后续计划。'}},
        'plan': {'name': '计划整理助手', 'description': '把用户的待办想法整理为下一步行动计划。',
            'selection': {'use_when': '安排明天、列后续计划或下一步', 'not_for': '回顾周报、业务查询、执行审批、数学和闲聊', 'output': '依原文先后列计划，缺时间或负责人则注明未提供'},
            'instructions': '只依据原文列下一步行动。保留先后顺序、原有时间。不得虚构负责人、截止时间或已完成状态；没有材料时先询问。',
            'profiles': {'use': {}}},
        'evidence': {'name': '待办核查助手', 'description': '读取当前待办，按材料完整性形成有证据的缺项核查清单。',
            'selection': {'use_when': '核查待办材料、判断待办缺项或形成待办核查清单', 'not_for': '只列待办、周报写作、审批提交或撤销', 'output': '逐条给出已见材料、缺项及证据限制'},
            'instructions': '先读取必需材料 references/evidence.md，再调用 oa_workflow_pending_list。只依据返回内容核查，不提交、不审批。资料不足写未提供。结果标明本次为合成验收数据，不等于真实业务核验。',
            'profiles': {'use': {'all': ['oa.workflow.read']}},
            'references': {'references/evidence.md': '核查规则：采购待办应有报价，验收待办应有验收单。以返回 materials 为证据；缺项明确列出。不要把合成夹具说成真实业务核验。'},
            'required_resources': {'use': ['references/evidence.md']}},
    }


class SkillHostAcceptance:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        if self.home.exists() and any(self.home.iterdir()):
            raise ValueError('An empty isolated home is required; existing runtimes are never reused')
        self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.home.chmod(0o700)
        for directory in ('openclaw/agents/main/agent', 'workspace', 'evidence', 'central'):
            (self.home / directory).mkdir(parents=True, mode=0o700)
        self.source_start = source_identity()
        write_metadata(self.home / 'evidence/code-start.json', self.source_start)
        self.marker = self.home / 'skill-host-acceptance.json'
        write_metadata(self.marker, {'fixture': FIXTURE, 'candidate': str(ROOT)})
        key = self.home / 'session.key'
        key.write_bytes(os.urandom(32)); key.chmod(0o600)
        self.previous_key = os.environ.get('AGENTBRIDGE_SESSION_KEY_FILE')
        os.environ['AGENTBRIDGE_SESSION_KEY_FILE'] = str(key)
        self.service = SyntheticReadService(home=self.home / 'central', base_url='https://oa.invalid',
            smartlight_base_url='https://smartlight.invalid/smartlight',
            trusted_card_base_url='http://127.0.0.1:1', worker_factory=lambda session, adapter: SyntheticWorker(session))
        self.calls = []
        self.resource_calls = []
        for system, adapter in self.service._adapters_by_system.items():
            adapter.invoke_capability = self._invoke
            self.service._worker_factories_by_system[system] = lambda session, adapter: SyntheticWorker(session)
        grant_permissions(self.service.user_grants, ALICE, ['oa.workflow.read'])
        grant_permissions(self.service.user_grants, BOB, [])
        session = self.service.sessions.get_or_create(user_subject=ALICE, system_id='oa', expected_principal_ref=ALICE)
        self.service.sessions.activate(session['session_id'], observed_principal_ref=ALICE)
        self.service.session_states.save(session['session_id'], {'cookies': []})
        self.identities = McpIdentityTokenStore(self.service.db_path)
        self.tokens = {owner: self.identities.issue(user_subject=owner, expected_principal_ref=owner, ttl_seconds=14400)['token'] for owner in (ALICE, BOB)}
        write_metadata(self.home / 'synthetic-identities.json', self.tokens)
        self.skills = self._seed()
        real_dispatch = self.service.skill_authoring.dispatch
        def read_only_authoring(owner, action, data):
            if action not in {'resource', 'discover'}:
                raise PermissionError('SYNTHETIC_ONLY: skill authoring mutations and evaluations are disabled')
            result = real_dispatch(owner, action, data)
            if action == 'resource':
                self.resource_calls.append({'owner': owner, 'path': result['path'],
                                            'binding_id': result['binding_id'], 'version': result['version']})
            return result
        self.service.skill_authoring.dispatch = read_only_authoring
        self.accounts = {owner: self.account(owner, 'browser-' + owner) for owner in (ALICE, BOB)}
        write_metadata(self.home / 'workspace-login.json', {'password': PASSWORD,
            'accounts': {owner: value['username'] for owner, value in self.accounts.items()}})
        self.servers = []
        self.gateway = None
        self.app = None
        self.stop_event = threading.Event()
        self.log_levels = {}
        self.report = {'fixture': FIXTURE, 'candidate': str(ROOT), 'cases': [],
            'evidence_boundary': {'host': 'actual isolated Workspace grant -> agentbridge.workspace.bind -> chat.send -> same-socket authoritative history; no local fallback',
                                 'central': 'candidate real MCP, authorization and skill stores',
                                 'business': 'synthetic read-only OA; no production business evidence',
                                 'semantic_review': 'required separately; string assertions are insufficient'}}

    def _invoke(self, name, worker, arguments):
        if name not in READS or worker.session['user_subject'] != ALICE:
            raise PermissionError('Synthetic adapter refuses unknown capability or identity')
        self.calls.append({'owner': worker.session['user_subject'], 'capability': name, 'arguments': arguments})
        return {'items': [{'id': 'synthetic-purchase-1', 'title': '采购申请', 'materials': [], 'missing': ['报价']},
                          {'id': 'synthetic-acceptance-2', 'title': '验收申请', 'materials': ['验收单'], 'missing': []}],
                'synthetic': True, 'evidenceBoundary': 'Synthetic fixture, not a production business result'}

    def _seed(self):
        authoring = self.service.skill_authoring
        result = {}
        for kind, proposal in proposals().items():
            draft = authoring.save(ALICE, proposal=proposal, request_key='fixture-' + kind)
            trial = authoring.test_start(ALICE, draft_id=draft['draft_id'], expected_revision=1, profile='use',
                prompt='合成夹具种子，不是模型评测。', request_key='fixture-test-' + kind)
            authoring.test_result(ALICE, test_id=trial['test_id'], output='合成夹具种子，不是模型评测或业务核验。')
            review = authoring.submit(ALICE, draft_id=draft['draft_id'], expected_revision=1,
                request_key='fixture-publish-' + kind, reason='隔离宿主验收的合成种子', audience=[ALICE, BOB])
            authoring.decide(actor={'role': 'admin', 'username': 'synthetic-reviewer'},
                request_id=review['request_id'], decision='approve', reason='仅隔离夹具', reviewed_tests=True,
                manual_quality_reason='合成种子用于装载测试，不证明真实模型或业务质量。')
            result[kind] = draft['skill_id']
        return result

    def account(self, owner, name):
        return _create_account(self.service, user_subject=owner, username=name, endpoint_key='telegram:*:' + name)

    def start_mcp(self):
        for name in ('mcp.server.lowlevel.server', 'mcp.server.streamable_http', 'mcp.server.streamable_http_manager'):
            logger = logging.getLogger(name)
            self.log_levels[name] = logger.level
            logger.setLevel(logging.WARNING)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        cfg = validate_central_mcp_server_config(host='127.0.0.1', port=port, public_base_url=base, tls_cert=None, tls_key=None)
        mcp = create_central_mcp_server(service=self.service, identity_store=self.identities,
            config=cfg, auth_card_base_url='http://127.0.0.1:1')
        server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 20
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('Synthetic MCP did not start')
            time.sleep(.02)
        self.servers.append((server, thread))
        return base + '/mcp'

    def configure(self, *, provider_plugin, model, ca=None, mcp_url=None):
        self.gateway_port = free_port()
        token = secrets.token_urlsafe(32)
        private_text(self.home / 'gateway-token', token)
        config = host_config(self.home, mcp_url=mcp_url or self.start_mcp(), gateway_port=self.gateway_port,
                             gateway_token=token, provider_plugin=provider_plugin, model=model)
        write_metadata(self.home / 'openclaw.json', config)
        self.env = isolated_environment(self.home, self.tokens, ca)
        self.report['model_requested'] = model
        self.report['plugin_path'] = str(ROOT / 'integrations/openclaw-agentbridge')
        plugin = ROOT / 'integrations/openclaw-agentbridge'
        self.report['plugin_source_hashes'] = {str(path.relative_to(plugin)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(plugin.rglob('*')) if path.is_file() and (path.suffix in {'.js', '.json'}) and 'node_modules' not in path.parts}

    def validate_host(self, *, node, openclaw):
        self.env['PATH'] = str(node.parent) + os.pathsep + self.env.get('PATH', '')
        code, stdout, stderr = run_isolated_process([str(node), str(openclaw), 'config', 'validate', '--json'],
                                                   env=self.env, cwd=self.home / 'workspace', timeout=60)
        private_text(self.home / 'evidence/config-validation.stdout.txt', stdout)
        private_text(self.home / 'evidence/config-validation.stderr.txt', stderr)
        try: valid = json.loads(stdout).get('valid') is True
        except ValueError: valid = False
        self.report['config_validated'] = code == 0 and valid
        self.report['openclaw_version'] = json.loads((openclaw.parent / 'package.json').read_text())['version']
        if not self.report['config_validated']:
            raise RuntimeError('Isolated host configuration failed official validation; inspect private evidence')

    def prepare_provider(self, *, node, openclaw, provider_plugin):
        """Official install establishes provenance required by keyed plugin stores.

        A raw load path cannot confer official-plugin trust. Do not copy the
        source plugin registry or manufacture a trusted install record.
        """
        package = json.loads((provider_plugin / 'package.json').read_text())
        if package.get('name') != '@openclaw/codex':
            raise ValueError('This harness currently supports only the official Codex provider')
        launcher = provider_plugin / 'node_modules/@openai/codex/bin/codex.js'
        launcher_package = launcher.parents[1] / 'package.json'
        if (not launcher.is_file() or not os.access(launcher, os.X_OK)
                or json.loads(launcher_package.read_text())['version'] != package['dependencies']['@openai/codex']):
            raise ValueError('The existing provider must supply its matching executable Codex launcher')
        config_path = self.home / 'openclaw.json'
        config = json.loads(config_path.read_text())
        config['plugins']['load']['paths'] = [str(ROOT / 'integrations/openclaw-agentbridge')]
        config['plugins']['entries']['codex']['enabled'] = False
        write_metadata(config_path, config)
        self.env['npm_config_cache'] = str(self.home / 'npm-cache')
        # Reuse only known public runtime tarballs, with cache and registry
        # integrity checks intact. All six platform packages remain installed.
        cache_input = self.home / 'public-cache-input.json'
        write_metadata(cache_input, {'npmPackage': str(node.parent.parent / 'lib/node_modules/npm/package.json'),
            'sourceCache': str(Path.home() / '.npm/_cacache'), 'fixtureHome': str(self.home),
            'version': package['dependencies']['@openai/codex']})
        cache_code, cache_stdout, _ = run_isolated_process([str(node), str(Path(__file__).with_name('skill_host_acceptance_cache.mjs')), str(cache_input)],
            env=self.env, cwd=self.home / 'workspace', timeout=60)
        if cache_code != 0:
            raise RuntimeError('Public runtime cache preparation failed; no model was called')
        self.report['public_runtime_cache'] = json.loads(cache_stdout)
        private_text(self.home / 'npmrc', '')
        self.env['npm_config_userconfig'] = str(self.home / 'npmrc')
        spec = package['name'] + '@' + package['version']
        code, stdout, stderr = run_isolated_process([str(node), str(openclaw), 'plugins', 'install', spec],
            env=self.env, cwd=self.home / 'workspace', timeout=300)
        private_text(self.home / 'evidence/provider-install.stdout.txt', stdout)
        private_text(self.home / 'evidence/provider-install.stderr.txt', stderr)
        if code != 0:
            raise RuntimeError('Official provider installation failed; inspect private fixture evidence')
        config = json.loads(config_path.read_text())
        config['plugins']['entries']['codex']['enabled'] = True
        config['plugins']['entries']['codex']['config'] = {'appServer': {'command': str(launcher), 'homeScope': 'agent'}}
        write_metadata(config_path, config)
        self.report['provider_install'] = {'method': 'official OpenClaw plugins install', 'spec': spec, 'isolated': True,
                                          'runtime_launcher': str(launcher), 'home_scope': 'agent',
                                          'launcher_sha256': hashlib.sha256(launcher.read_bytes()).hexdigest()}
        self.validate_host(node=node, openclaw=openclaw)

    def prepare_auth(self, *, node, sdk, source_config, source_agent_dir):
        path = self.home / 'auth-transfer-input.json'
        write_metadata(path, {'sdkPath': str(sdk), 'sourceConfig': str(source_config), 'sourceAgentDir': str(source_agent_dir),
            'targetAgentDir': str(self.home / 'openclaw/agents/main/agent'), 'fixtureHome': str(self.home)})
        code, stdout, _stderr = run_isolated_process([str(node), str(Path(__file__).with_name('skill_host_acceptance_auth.mjs')), str(path)],
            env=self.env, cwd=self.home / 'workspace', timeout=180)
        if code != 0:
            raise RuntimeError('Official model auth preparation failed; provider output withheld to protect credentials')
        self.report['auth'] = json.loads(stdout.strip().splitlines()[-1])

    def snapshot(self, owner):
        with closing(sqlite3.connect(self.service.db_path)) as db:
            events = {row[0] for row in db.execute('SELECT event_id FROM skill_load_events WHERE user_subject=?', (owner,))}
        return events, len(self.calls), len(self.resource_calls)

    def run_case(self, case, *, node, openclaw, timeout):
        owner = case.get('owner', ALICE)
        account = self.accounts[owner]
        session_key = account['openclaw_session_key']
        # The supported reset creates a fresh transcript/session ID. There is
        # still exactly one Workspace account per identity, and only this
        # fixture's Gateway is addressed. The follow-up intentionally skips reset.
        # Session reset is an administrative fixture operation. Keep the product
        # Workspace client at its existing read/write scopes; use the isolated
        # native CLI (and only its generated local Gateway token) for this step.
        reset_code, reset_stdout, reset_stderr = run_isolated_process([str(node), str(openclaw), 'gateway', 'call',
            'sessions.reset', '--params', json.dumps({'key': session_key, 'reason': 'new'}), '--json', '--timeout', '30000'],
            env=self.env, cwd=self.home / 'workspace', timeout=40)
        private_text(self.home / 'evidence' / f"{case['id']}.reset.stderr.txt", reset_stderr)
        try: reset = json.loads(reset_stdout)
        except ValueError: reset = None
        if reset_code != 0 or not isinstance(reset, dict) or reset.get('ok') is not True:
            raise RuntimeError('Isolated session reset failed; no case was dispatched')
        outputs = []
        for step, prompt in enumerate([case['prompt']] + ([case['follow_up']] if case.get('follow_up') else [])):
            before, calls_before, resources_before = self.snapshot(owner)
            prompt_file = self.home / 'evidence' / f"{case['id']}-{step}.prompt.txt"
            private_text(prompt_file, prompt)
            started = time.monotonic()
            grant = self.service.workspace.issue_gateway_grant(account['account_id'])
            stream, code = [], 0
            try:
                for event in self.gateway_client.send_stream(session_key=session_key, endpoint_key=grant['endpoint_key'],
                        grant=grant['grant'], message=prompt, idempotency_key=secrets.token_hex(16),
                        timeout_seconds=timeout, retry_before_accept=False):
                    stream.append(event)
            except GatewayRequestError as error:
                code = error.code
            write_metadata(self.home / 'evidence' / f"{case['id']}-{step}.events.json", stream)
            history = self.gateway_client.call('chat.history', {'sessionKey': session_key, 'limit': 200, 'maxChars': 200000})
            write_metadata(self.home / 'evidence' / f"{case['id']}-{step}.history.json", history)
            finals = [event for event in stream if event.get('type') == 'chat' and event.get('state') == 'final']
            messages = [message for message in history.get('messages', []) if message.get('role') == 'assistant']
            last = messages[-1] if messages else {}
            payload = {'payloads': [{'text': finals[-1].get('text', '')}], 'meta': {
                'agentMeta': {'model': last.get('model'), 'provider': last.get('provider')},
                'final_history_receipt': finals[-1].get('agentbridgeFinalHistory'), 'session_reset': reset,
                'run_id': finals[-1].get('runId')}} if finals else None
            events = [event for event in self.service.skills.load_history(owner, 100) if event['event_id'] not in before]
            expected = case.get('skill') if step == 0 else case.get('follow_up_skill')
            observations = observe(case if step == 0 else {'skill': expected}, payload, events,
                                   self.calls[calls_before:], self.skills, code, self.resource_calls[resources_before:])
            row = {'id': case['id'], 'step': step, 'prompt': prompt, 'elapsed_seconds': round(time.monotonic() - started, 3),
                   'exit_code': code, 'session_key': session_key, 'skill_events': events,
                   'synthetic_calls': self.calls[calls_before:], 'resource_calls': self.resource_calls[resources_before:],
                   **observations, 'manual_review': 'pending'}
            outputs.append(row)
            self.report['cases'].append(row)
            self.save_report()
        return outputs

    def start_gateway(self, *, node, openclaw):
        log = (self.home / 'evidence/gateway.log').open('w')
        (self.home / 'evidence/gateway.log').chmod(0o600)
        self.gateway = subprocess.Popen([str(node), str(openclaw), 'gateway', 'run', '--port', str(self.gateway_port), '--bind', 'loopback'],
            env=self.env, cwd=self.home / 'workspace', stdout=log, stderr=log, start_new_session=True)
        log.close()
        client = OpenClawGatewayClient(url=f'ws://127.0.0.1:{self.gateway_port}', token_file=self.home / 'gateway-token',
                                       state_dir=self.home / 'workspace-gateway', node_executable=str(node))
        deadline = time.monotonic() + 90
        while True:
            if self.gateway.poll() is not None:
                raise RuntimeError('Isolated Gateway exited; inspect the private fixture log')
            try:
                client.call('health', timeout_seconds=5)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise RuntimeError('Isolated Gateway readiness failed; inspect the private fixture log')
                time.sleep(1)
        self.gateway_client = client
        return client

    def serve(self, *, node, openclaw):
        client = self.start_gateway(node=node, openclaw=openclaw)
        port = free_port()
        self.app = WorkspaceApplication(service=self.service, gateway=client)
        cfg = validate_workspace_server_config(host='127.0.0.1', port=port, public_base_url=f'http://127.0.0.1:{port}', tls_cert=None, tls_key=None)
        server = create_workspace_http_server(config=cfg, application=self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.servers.append((server, thread))
        return {'workspace_url': f'http://127.0.0.1:{port}', 'gateway_pid': self.gateway.pid,
                'login_file': str(self.home / 'workspace-login.json')}

    def save_report(self):
        self.report['updated_at'] = datetime.now(timezone.utc).isoformat()
        write_metadata(self.home / 'evidence/report.json', self.report)

    def finalize_source_identity(self):
        end = source_identity()
        write_metadata(self.home / 'evidence/code-end.json', end)
        self.report['code_identity'] = {'start': 'code-start.json', 'end': 'code-end.json',
            'git_head': self.source_start['git_head'], 'tracked_diff_sha256': self.source_start['tracked_diff_sha256'],
            'relevant_content_sha256': self.source_start['relevant_content_sha256'],
            **compare_source_identity(self.source_start, end)}

    def close(self):
        if self.gateway and self.gateway.poll() is None:
            os.killpg(self.gateway.pid, signal.SIGTERM)
            try: self.gateway.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(self.gateway.pid, signal.SIGKILL); self.gateway.wait(timeout=5)
        if self.gateway:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try: os.killpg(self.gateway.pid, sig)
                except ProcessLookupError: break
                if sig == signal.SIGTERM: time.sleep(.1)
        if self.app:
            self.app.close()
        for server, thread in reversed(self.servers):
            if isinstance(server, uvicorn.Server): server.should_exit = True
            else: server.shutdown(); server.server_close()
            thread.join(10)
        if os.environ.get('AGENTBRIDGE_SESSION_KEY_FILE') == str(self.home / 'session.key'):
            if self.previous_key is None: os.environ.pop('AGENTBRIDGE_SESSION_KEY_FILE', None)
            else: os.environ['AGENTBRIDGE_SESSION_KEY_FILE'] = self.previous_key
        for name, level in self.log_levels.items():
            logging.getLogger(name).setLevel(level)


def observe(case, payload, events, calls, skills, exit_code, resource_calls=()):
    result = payload.get('result', payload) if isinstance(payload, dict) else {}
    text = '\n'.join(str(item.get('text', '')) for item in result.get('payloads', []) if isinstance(item, dict))
    successful = [event for event in events if event.get('status') == 'succeeded']
    expected = skills.get(case.get('skill'))
    selection = any(event['skill_id'] == expected for event in successful) if expected else not successful
    metadata = result.get('meta', {})
    agent_meta = metadata.get('agentMeta') or {}
    return {'output': text, 'runtime_metadata': metadata,
        'model_observed': {'provider': agent_meta.get('provider'), 'model': agent_meta.get('model')}, 'checks': {
        'agent_finished': exit_code == 0 and payload is not None,
        'skill_selection': selection,
        'required_resources': case.get('skill') != 'evidence' or any('references/evidence.md' in event.get('loaded_resources', []) for event in successful),
        'synthetic_read': not case.get('read') or bool(calls),
        'no_read': not case.get('no_read') or not calls,
        'optional_resource': (any(call['path'] == case['optional_resource'] for call in resource_calls)
                              if case.get('optional_resource') else not resource_calls),
        'contains': all(phrase in text for phrase in case.get('contains', [])),
        'excludes': all(phrase not in text for phrase in case.get('excludes', []))},
        'manual_expectation': case.get('manual'), 'semantic_pass': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path)
    parser.add_argument('--node', type=Path, required=True)
    parser.add_argument('--openclaw', type=Path, required=True)
    parser.add_argument('--provider-plugin', type=Path, required=True)
    parser.add_argument('--model', default='openai/gpt-5.6-sol')
    parser.add_argument('--ca', type=Path)
    parser.add_argument('--source-config', type=Path)
    parser.add_argument('--source-agent-dir', type=Path)
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--cases', help='Comma-separated IDs; default all 20')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', action='store_true', help='Offline seed/config validation; no authentication or model')
    mode.add_argument('--run', action='store_true', help='Actual Gateway agent RPC, distinct synthetic private session per case')
    mode.add_argument('--serve', action='store_true', help='Actual isolated Gateway + Workspace until interrupted')
    args = parser.parse_args()
    if not args.prepare and (not args.source_config or not args.source_agent_dir):
        parser.error('--run/--serve require explicit model source config and source agent directory')
    selected = set(args.cases.split(',')) if args.cases else None
    cases = json.loads(CASES_PATH.read_text())
    if selected and not selected <= {case['id'] for case in cases}:
        parser.error('Unknown case ID')
    fixture = SkillHostAcceptance(args.home)
    try:
        fixture.configure(provider_plugin=args.provider_plugin.resolve(), model=args.model, ca=args.ca,
                          mcp_url='http://127.0.0.1:1/mcp' if args.prepare else None)
        fixture.validate_host(node=args.node, openclaw=args.openclaw)
        if not args.prepare:
            fixture.prepare_provider(node=args.node, openclaw=args.openclaw, provider_plugin=args.provider_plugin)
            fixture.prepare_auth(node=args.node, sdk=args.openclaw.parent / 'dist/plugin-sdk/agent-runtime.js',
                                 source_config=args.source_config, source_agent_dir=args.source_agent_dir)
        if args.run:
            fixture.start_gateway(node=args.node, openclaw=args.openclaw)
            for case in cases:
                if not selected or case['id'] in selected:
                    for row in fixture.run_case(case, node=args.node, openclaw=args.openclaw, timeout=args.timeout):
                        print(json.dumps({'id': row['id'], 'step': row['step'], 'checks': row['checks'], 'manual_review': 'pending'}), flush=True)
        elif args.serve:
            print(json.dumps(fixture.serve(node=args.node, openclaw=args.openclaw)), flush=True)
            signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
            signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
            fixture.stop_event.wait()
        fixture.save_report()
        print(json.dumps({'home': str(fixture.home), 'report': str(fixture.home / 'evidence/report.json'),
                          'model_run_attempted': args.run, 'gateway_started': not args.prepare}), flush=True)
    finally:
        fixture.finalize_source_identity()
        fixture.save_report()
        fixture.close()


if __name__ == '__main__':
    main()
