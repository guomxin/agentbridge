import hashlib
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]



def test_secret_scanner_detects_patterns_without_returning_secret_values():
    from scripts.check_public_content import findings
    assert findings('safe\n' + 'AKIA' + 'A' * 16) == [2]
    assert findings('a normal configuration value') == []


def test_worktree_scan_covers_untracked_text_and_unstaged_deletions(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    import pytest
    from scripts import check_public_content

    subprocess.run(['git', 'init', '-q', tmp_path], check=True)
    deleted = tmp_path / 'removed.md'
    deleted.write_text('old text')
    subprocess.run(['git', '-C', tmp_path, 'add', 'removed.md'], check=True)
    deleted.unlink()
    draft = tmp_path / 'new.md'
    draft.write_text('ghp_' + 'X' * 32)
    monkeypatch.setattr(check_public_content, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['check_public_content.py', '--worktree'])
    with pytest.raises(SystemExit) as error:
        check_public_content.main()
    assert str(error.value) == 'new.md:1: credential pattern'
    draft.write_text('updated text')
    check_public_content.main()
    assert 'passed' in capsys.readouterr().out
