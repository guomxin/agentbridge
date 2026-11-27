"""The second native process must import only the library in its own release."""
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/nanobot-agentbridge'))
from nanobot_agentbridge.worker_library import BOOTSTRAP,release_library


def test_real_isolated_subprocess_uses_own_wheel_and_never_workspace_fallback(tmp_path):
    release=tmp_path/'release';release.mkdir()
    host=release/'nanobot_agentbridge';host.mkdir();(host/'__init__.py').write_text('')
    (host/'worker.py').write_text('import agentbridge\nprint(agentbridge.SOURCE)\n')
    wheel=release/'agentbridge.whl'
    with zipfile.ZipFile(wheel,'w') as archive:
        archive.writestr('agentbridge/__init__.py','SOURCE="original-release"\n')
    workspace=tmp_path/'unrelated';workspace.mkdir();(workspace/'agentbridge').mkdir()
    (workspace/'agentbridge/__init__.py').write_text('raise AssertionError("workspace imported")\n')
    command=[sys.executable,'-I','-B','-c',BOOTSTRAP,str(release),str(release_library(release))]
    result=subprocess.run(command,cwd=workspace,env={**os.environ,'PYTHONPATH':str(workspace)},capture_output=True,text=True)
    assert result.returncode==0 and result.stdout.strip()=='original-release'
    with zipfile.ZipFile(wheel,'w') as archive: archive.writestr('unrelated.txt','no central package')
    failed=subprocess.run(command,cwd=workspace,env={**os.environ,'PYTHONPATH':str(workspace)},capture_output=True,text=True)
    assert failed.returncode!=0 and 'original-release' not in failed.stdout


def test_missing_linked_or_writable_release_library_is_not_used(tmp_path):
    with pytest.raises(FileNotFoundError): release_library(tmp_path)
    target=tmp_path/'elsewhere.whl';target.write_bytes(b'unused')
    wheel=tmp_path/'agentbridge.whl';wheel.symlink_to(target)
    with pytest.raises(PermissionError): release_library(tmp_path)
    wheel.unlink();wheel.write_bytes(b'unused');wheel.chmod(0o666)
    with pytest.raises(PermissionError): release_library(tmp_path)
