"""Fail on high-confidence reusable credentials; print locations, never values.

This is a minimal guard, not proof that business evidence is safe to publish.
CI scans tracked text. --worktree also checks new, non-ignored local files.
"""
import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{50,}\b'),
    re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
)


def findings(text):
    return [number for number, line in enumerate(text.splitlines(), 1)
            if any(pattern.search(line) for pattern in PATTERNS)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worktree', action='store_true', help='Include non-ignored, untracked local files')
    worktree = parser.parse_args().worktree
    metadata = '.gitrepo' if (ROOT / '.gitrepo').exists() else '.git'
    raw = subprocess.check_output(['git', f'--git-dir={ROOT / metadata}', f'--work-tree={ROOT}',
                                   'ls-files', '-z', *(['--cached', '--others', '--exclude-standard'] if worktree else [])], cwd=ROOT)
    errors = []
    for name in raw.decode('utf-8').split('\0'):
        if not name:
            continue
        path = ROOT / name
        if worktree:
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                errors.append(f'{name}: cannot scan a linked path')
                continue
            if not path.exists():
                continue  # Unstaged deletion, still listed in the index.
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        errors.extend(f'{name}:{line}: credential pattern' for line in findings(text))
    if errors:
        raise SystemExit('\n'.join(errors))
    scope = 'Working tree' if worktree else 'Tracked'
    print(f'{scope} public-content credential patterns: passed (not a business-data audit)')


if __name__ == '__main__':
    main()
