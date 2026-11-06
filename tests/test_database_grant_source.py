import json
from contextlib import closing
from dataclasses import asdict
import sqlite3
import sys
from unittest.mock import patch

import pytest

from agentbridge.core.data_source_secrets import DataSourceSecretStore
from agentbridge.database.independent import IndependentDatabase, main
from agentbridge.database.legacy_source import LegacySourceConfig
from agentbridge.database.sources import Sources
from tests.database_fixtures import configured_source


def grant(home, *options):
    with patch.object(sys, 'argv', ['database', '--home', str(home), '--subject', 'reader', 'grant', *options]):
        main()


@pytest.mark.parametrize('sources', [('taihua_primary',), ('team_logs',), ('taihua_primary', 'team_logs')])
@pytest.mark.parametrize('capabilities', [[], ['database.free.read']])
def test_grant_without_source_never_modifies_any_grant(tmp_path, capsys, sources, capabilities):
    runtime = IndependentDatabase(tmp_path)
    for sid in sources:
        configured_source(tmp_path, sid)
        runtime.grants.set('reader', ['database.schema'], source_id=sid)
    before = {sid: runtime.grants.get('reader', sid) for sid in ('taihua_primary', 'team_logs')}

    with pytest.raises(SystemExit) as error:
        grant(tmp_path, '--capabilities', *capabilities)

    assert error.value.code == 2
    assert '--source-id' in capsys.readouterr().err
    assert {sid: runtime.grants.get('reader', sid) for sid in before} == before


def test_explicit_grant_and_revocation_only_modify_the_chosen_source(tmp_path, capsys):
    runtime = IndependentDatabase(tmp_path)
    for sid in ('taihua_primary', 'team_logs'):
        configured_source(tmp_path, sid)
        runtime.grants.set('reader', ['database.schema'], source_id=sid)
    original = runtime.grants.get('reader', 'taihua_primary')

    for capabilities in (['database.logs.analyze'], []):
        grant(tmp_path, '--source-id', 'team_logs', '--capabilities', *capabilities)
        result = json.loads(capsys.readouterr().out)
        assert result['capabilities'] == capabilities
        assert runtime.grants.get('reader', 'team_logs') == result
        assert runtime.grants.get('reader', 'taihua_primary') == original


def test_missing_source_cannot_initialize_a_fresh_grants_store(tmp_path):
    home = tmp_path / 'uninitialized'
    with pytest.raises(SystemExit) as error:
        grant(home)
    assert error.value.code == 2
    assert not home.exists()

    with pytest.raises(SystemExit) as error:
        grant(home, '--source-id', 'unregistered', '--capabilities', 'database.schema')
    assert error.value.code == 2
    assert not (home / 'database' / 'grants.sqlite3').exists()


@pytest.mark.parametrize('capabilities', [[], ['database.schema']])
def test_unknown_source_cannot_leave_a_grant_for_a_future_source(tmp_path, capsys, capabilities):
    configured_source(tmp_path, 'team_logs')
    runtime = IndependentDatabase(tmp_path)
    before = runtime.grants.set('reader', ['database.schema'], source_id='team_logs')

    with pytest.raises(SystemExit) as error:
        grant(tmp_path, '--source-id', 'team_log', '--capabilities', *capabilities)
    assert error.value.code == 2
    assert '数据源不存在' in capsys.readouterr().err
    with closing(sqlite3.connect(runtime.grants.path)) as conn:
        assert conn.execute("SELECT count(*) FROM source_grants WHERE source='team_log'").fetchone()[0] == 0
    configured_source(tmp_path, 'team_log')
    assert [s['source_id'] for s in runtime.catalog('reader')['sources']] == ['team_logs']
    assert runtime.grants.get('reader', 'team_logs') == before


def test_known_generic_source_rejects_unsupported_log_capability(tmp_path, capsys):
    configured_source(tmp_path, 'devices', pack='generic')
    runtime = IndependentDatabase(tmp_path)
    before = runtime.grants.set('reader', ['database.schema'], source_id='devices')
    with pytest.raises(SystemExit) as error:
        grant(tmp_path, '--source-id', 'devices', '--capabilities', 'database.schema', 'database.logs.analyze')
    assert error.value.code == 2
    assert '不支持所选能力' in capsys.readouterr().err
    assert runtime.grants.get('reader', 'devices') == before


def test_grant_recognizes_an_existing_legacy_configuration_before_first_query(tmp_path, capsys):
    legacy = tmp_path / 'analytics'
    legacy.mkdir()
    config = LegacySourceConfig(enabled=True, username='fixture', privileges_reviewed=True)
    (legacy / 'source.json').write_text(json.dumps(asdict(config)))
    DataSourceSecretStore(legacy / 'credentials').save('taihua_primary:1', {'password':'fixture-only'})

    grant(tmp_path, '--source-id', 'taihua_primary', '--capabilities', 'database.logs.analyze')
    result = json.loads(capsys.readouterr().out)
    assert result['capabilities'] == ['database.logs.analyze']
    assert Sources(tmp_path).get('taihua_primary')['migration'] == 'legacy-v1'
