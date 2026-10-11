"""Small, deterministic candidate ranking; never decides access or applicability.

Keep the host's skill-ranking.js in sync through tests/fixtures/skill_ranking.json.
Exclusion text is deliberately absent from positive relevance scoring.
"""
from __future__ import annotations

import re


def _text(value):
    return ' '.join(value.lower().split()) if isinstance(value, str) else ''


def _grams(value):
    value = re.sub(r'\s+', '', _text(value))
    return {value[i:i + 2] for i in range(len(value) - 1)}


def _mentions(query, label):
    label = _text(label)
    if not label:
        return False
    # IDs and Latin names must not match inside a longer identifier (s1/s10,
    # oa/x-oa). Chinese names can occur next to ordinary Chinese request text.
    left = r'(?<![a-z0-9_-])' if re.match(r'[a-z0-9_-]', label[0]) else ''
    right = r'(?![a-z0-9_-])' if re.match(r'[a-z0-9_-]', label[-1]) else ''
    return re.search(left + re.escape(label) + right, query) is not None


def rank_skill_candidates(items, query=''):
    query = _text(query)
    query_grams = _grams(query)

    def score(item):
        selection = item.get('selection') or {}
        explicit = _mentions(query, item.get('id')) or _mentions(query, item.get('name'))
        positive = set()
        for field in (item.get('name'), item.get('description'),
                      selection.get('use_when'), selection.get('output')):
            positive.update(_grams(field))
        return explicit, len(query_grams & positive)

    # Python's stable sort preserves catalog order for empty queries and ties.
    return sorted(items, key=score, reverse=True)
