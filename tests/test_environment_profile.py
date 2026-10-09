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
