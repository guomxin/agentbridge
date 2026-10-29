"""Bounded progress and provider-reported usage, without inventing missing totals."""
from __future__ import annotations

import math


USAGE_FIELDS = ('input', 'output', 'totalTokens', 'cacheRead', 'cacheWrite', 'cost_estimate_usd')


def _number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False  # Arbitrarily large Python integers are not JSON-client numbers.


def _sum(values):
    total = 0
    for value in values:
        total += value
        if not _number(total):
            return None  # A missing aggregate is safer than a nonfinite or truncated total.
    return total


def progress_stats(kind, payload, rows, *, uncertain=False):
    """Completeness concerns recorded responses; failed leases may hide other calls."""
    calls = [row for row in rows if not row.get('skipped') and
             ('output' in row or row.get('kind') == 'generation_call')]
    values = {field: [row['usage'][field] for row in calls
                      if isinstance(row.get('usage'), dict) and _number(row['usage'].get(field))]
              for field in USAGE_FIELDS}
    usage = {field: _sum(found) if found else None for field, found in values.items()}
    token_fields = ('input', 'output', 'totalTokens')
    usage_calls = sum(isinstance(row.get('usage'), dict) and
                      any(_number(row['usage'].get(field)) for field in token_fields) for row in calls)
    complete_calls = sum(isinstance(row.get('usage'), dict) and
                         all(_number(row['usage'].get(field)) for field in token_fields) for row in calls)
    priced_calls = len(values['cost_estimate_usd'])
    total = None
    if kind != 'generation' and isinstance(payload.get('cases'), list) and isinstance(payload.get('repeats'), int):
        total = len(payload['cases']) * payload['repeats'] * (3 if payload.get('baseline') else 2)
    return {
        'completed': len(rows), 'total': total, 'model_calls': len(calls),
        'elapsed_ms': _sum(row['elapsed_ms'] for row in calls if _number(row.get('elapsed_ms'))),
        'usage': usage if any(values.values()) else None,
        'usage_calls': usage_calls, 'priced_calls': priced_calls,
        'usage_complete': bool(calls) and complete_calls == len(calls) and not uncertain
            and all(usage[field] is not None for field in token_fields),
        'cost_complete': bool(calls) and priced_calls == len(calls) and not uncertain
            and usage['cost_estimate_usd'] is not None,
        'unknown_usage_calls': len(calls) - complete_calls,
        'unknown_cost_calls': len(calls) - priced_calls,
        'unrecorded_calls_possible': uncertain,
    }
