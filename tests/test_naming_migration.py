"""Compatibility launches and state selection must not fork application identity."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from unittest.mock import patch

import pytest
from agentbridge.cli.main import main
from agentbridge.core.home import resolve_home
from bscli.cli.main import main as legacy_main

ROOT = Path(__file__).resolve().parents[1]


def test_new_install_default_and_explicit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch('pathlib.Path.home', return_value=tmp_path):
        assert resolve_home(None) == tmp_path / '.agentbridge'
        assert resolve_home('~/custom') == tmp_path / 'custom'
        assert not (tmp_path / '.agentbridge').exists()


@pytest.mark.parametrize('new_state', ['absent', 'pki-only', 'business'])
def test_existing_legacy_state_requires_explicit_choice_for_both_commands(tmp_path, new_state):
    legacy = tmp_path / '.bscli'
    legacy.mkdir()
    (legacy / 'systems').mkdir()
    if new_state != 'absent':
        (tmp_path / '.agentbridge' / ('pki' if new_state == 'pki-only' else 'systems')).mkdir(parents=True)
    with patch('pathlib.Path.home', return_value=tmp_path):
        for entry in (main, legacy_main):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert entry(['capability', 'list']) == 2
            assert json.loads(out.getvalue())['error']['code'] == 'HOME_SELECTION_REQUIRED'
        assert resolve_home(str(legacy)) == legacy


def test_dangling_legacy_link_is_not_treated_as_new_install(tmp_path):
    (tmp_path / '.bscli').symlink_to(tmp_path / 'missing')
    with patch('pathlib.Path.home', return_value=tmp_path), pytest.raises(ValueError):
        resolve_home(None)


def test_old_launcher_delegates_once_and_preserves_stdout(capsys):
    def run(argv):
        print(json.dumps({'status':'fixture', 'args':argv}))
        return 7
    with patch('agentbridge.cli.main.main', side_effect=run) as called:
        assert legacy_main(['--home', '/fixture']) == 7
    called.assert_called_once_with(['--home', '/fixture'])
    output = capsys.readouterr()
    assert json.loads(output.out)['status'] == 'fixture'
    assert '0.3.0' in output.err


def test_module_launchers_help_and_no_wholesale_import_aliases():
    for module in ('agentbridge', 'agentbridge.cli.main', 'bscli.cli.main'):
        result = subprocess.run([sys.executable, '-m', module, '--help'], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0
        assert 'usage: agentbridge' in result.stdout
        assert ('deprecated' in result.stderr) == (module == 'bscli.cli.main')
    with pytest.raises(ModuleNotFoundError):
        __import__('bscli.core')


def test_packaging_and_retired_platform_entries():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert config['project']['name'] == 'agentbridge'
    assert config['project']['scripts']['agentbridge'] == 'agentbridge.cli.main:main'
    assert not list((ROOT / 'scripts').glob('*.ps1'))
    assert not list((ROOT / 'scripts').glob('*.psm1'))
    assert not list((ROOT / 'scripts/migration').glob('*.py'))
    source = (ROOT / 'scripts/native/lifecycle.py').read_text()
    assert 'scripts/migration' not in source
    assert 'windowsGatewayStopped' not in source
