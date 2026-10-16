"""Native 2026.7.1 hook permissions and registration release gates, offline."""
import copy
import json
from pathlib import Path
import sys
from unittest import mock

import pytest
pytest.importorskip('fcntl')

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/native'))
import acceptance
import lifecycle


def config_with_hooks(value=True):
    return {'plugins': {'entries': {'agentbridge-interactions': {
        'enabled': True, 'hooks': {'allowConversationAccess': value}}}}}


@pytest.mark.parametrize('value', [None, False, 'true', 1])
def test_host_hook_permission_requires_explicit_boolean(value):
    with pytest.raises(ValueError, match='allowConversationAccess=true'):
        lifecycle.require_agentbridge_conversation_hooks(config_with_hooks(value))
    with pytest.raises(ValueError, match='allowConversationAccess=true'):
        lifecycle.require_agentbridge_conversation_hooks({})


def test_host_hook_validation_does_not_mutate_reviewed_config():
    config = config_with_hooks()
    before = copy.deepcopy(config)
    lifecycle.require_agentbridge_conversation_hooks(config)
    assert config == before


def test_settings_enforces_hook_permission_before_host_execution(tmp_path):
    profile = tmp_path / 'profile.json'
    profile.write_text('{}')
    profile.chmod(0o600)
    value = {'schema': 'agentbridge.mac-host.v1',
             **{name: str(tmp_path / name) for name in
                ('stateDir', 'configPath', 'envFile', 'runtimeDir', 'caCertificate')},
             'node': sys.executable, 'openclaw': sys.executable}
    for path in [tmp_path / 'configPath', tmp_path / 'envFile']:
        path.touch(mode=0o600)
    with mock.patch.object(lifecycle, 'read', side_effect=[value, config_with_hooks(False)]), \
         mock.patch.object(lifecycle, 'run') as execute:
        with pytest.raises(ValueError, match='allowConversationAccess=true'):
            lifecycle.settings(profile)
    execute.assert_not_called()


def test_runtime_native_inspection_needs_every_hook_and_permission():
    report = {'policy': {'allowConversationAccess': True},
              'typedHooks': [{'name': name} for name in acceptance.REQUIRED_AGENTBRIDGE_HOOKS]}
    acceptance.require_runtime_hooks(report)
    for missing in acceptance.REQUIRED_AGENTBRIDGE_HOOKS:
        incomplete = copy.deepcopy(report)
        incomplete['typedHooks'] = [h for h in incomplete['typedHooks'] if h['name'] != missing]
        with pytest.raises(ValueError, match='native runtime hooks'):
            acceptance.require_runtime_hooks(incomplete)
    report['policy']['allowConversationAccess'] = False
    with pytest.raises(ValueError, match='conversation access blocked'):
        acceptance.require_runtime_hooks(report)


@pytest.mark.parametrize('structured', [True, False])
def test_current_gateway_blocked_hook_is_rejected(tmp_path, structured):
    message = '[plugins] typed hook "agent_end" blocked because non-bundled plugins must set plugins.entries.agentbridge-interactions.hooks.allowConversationAccess=true'
    line = json.dumps({'time': '2026-10-16T12:00:01Z', 'message': message}) if structured else '2026-10-16T12:00:01Z ' + message
    (tmp_path / 'gateway.err.log').write_text(line + '\n')
    from datetime import datetime
    with pytest.raises(ValueError, match='Current Gateway blocked'):
        acceptance.require_gateway_hooks_unblocked({'runtimeDir': str(tmp_path)}, datetime.fromisoformat('2026-10-16T12:00:00+00:00').timestamp())


def test_historical_or_other_plugin_warning_does_not_reject_current_gateway(tmp_path):
    from datetime import datetime
    (tmp_path / 'gateway.out.log').write_text('\n'.join([
        '2026-10-16T11:00:00Z typed hook "agent_end" blocked plugin=agentbridge-interactions',
        '2026-10-16T12:00:01Z typed hook "agent_end" blocked plugin=another-plugin',
        '2026-10-16T12:00:02Z [gateway] ready',
    ]))
    acceptance.require_gateway_hooks_unblocked({'runtimeDir': str(tmp_path)}, datetime.fromisoformat('2026-10-16T12:00:00+00:00').timestamp())


def test_current_gateway_streams_required_for_hook_acceptance(tmp_path):
    with pytest.raises(ValueError, match='logs unavailable'):
        acceptance.require_gateway_hooks_unblocked({'runtimeDir': str(tmp_path)}, 0)
