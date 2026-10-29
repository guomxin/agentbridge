from agentbridge.core.skill_job_metrics import progress_stats


def test_skipped_rows_count_as_completed_but_not_model_calls_and_missing_usage_is_unknown():
    stats = progress_stats('evaluation', {'cases': [1, 2], 'repeats': 2, 'baseline': {}}, [
        {'key': 'skip', 'skipped': 'missing profile'}, {'key': 'actual', 'output': 'answer'},
    ])
    assert (stats['completed'], stats['total'], stats['model_calls']) == (2, 8, 1)
    assert stats['usage'] is None and stats['usage_calls'] == stats['priced_calls'] == 0
    assert stats['unknown_usage_calls'] == stats['unknown_cost_calls'] == 1
    assert not stats['usage_complete'] and not stats['cost_complete']


def test_only_finite_nonnegative_provider_values_are_aggregated_and_no_total_is_invented():
    stats = progress_stats('generation', {}, [
        {'kind': 'generation_call', 'usage': {'input': 10, 'output': 2, 'cacheRead': 3,
            'cacheWrite': 0, 'cost_estimate_usd': 0}, 'elapsed_ms': 9},
        {'kind': 'generation_call', 'usage': {'input': True, 'output': -1, 'totalTokens': float('inf'),
            'cacheRead': '2', 'cost_estimate_usd': float('nan')}, 'elapsed_ms': -1},
    ])
    assert stats['total'] is None and stats['elapsed_ms'] == 9
    assert stats['usage'] == {'input': 10, 'output': 2, 'totalTokens': None,
        'cacheRead': 3, 'cacheWrite': 0, 'cost_estimate_usd': 0}
    assert (stats['model_calls'], stats['usage_calls'], stats['priced_calls']) == (2, 1, 1)
    assert (stats['unknown_usage_calls'], stats['unknown_cost_calls']) == (2, 1)


def test_unknown_previous_attempt_keeps_known_cost_partial_after_success():
    stats = progress_stats('generation', {}, [{'kind': 'generation_call',
        'usage': {'input': 1, 'output': 2, 'totalTokens': 3, 'cost_estimate_usd': .2}}], uncertain=True)
    assert stats['usage']['cost_estimate_usd'] == .2
    assert stats['unknown_cost_calls'] == 0  # No fabricated count for an unobserved response.
    assert stats['unrecorded_calls_possible'] is True
    assert stats['usage_complete'] is False and stats['cost_complete'] is False


def test_unrepresentable_integers_are_unknown_without_breaking_json_statistics():
    import json
    stats = progress_stats('generation', {}, [{'kind': 'generation_call',
        'usage': {'input': 10 ** 1000, 'output': 2, 'cost_estimate_usd': 10 ** 1000}}])
    assert stats['usage']['input'] is None
    assert stats['usage']['output'] == 2
    assert stats['usage']['cost_estimate_usd'] is None
    assert stats['unknown_usage_calls'] == stats['unknown_cost_calls'] == 1
    assert stats['usage_complete'] is False and stats['cost_complete'] is False
    json.dumps(stats, allow_nan=False)


def test_finite_provider_values_cannot_overflow_into_a_complete_total():
    import json
    stats = progress_stats('generation', {}, [{'kind': 'generation_call', 'elapsed_ms': 1e308,
        'usage': {'input': 1e308, 'output': 1, 'totalTokens': 1e308, 'cost_estimate_usd': 1e308}}] * 2)
    assert stats['usage']['input'] is None and stats['usage']['totalTokens'] is None
    assert stats['usage']['output'] == 2
    assert stats['usage']['cost_estimate_usd'] is None and stats['elapsed_ms'] is None
    assert stats['usage_complete'] is False and stats['cost_complete'] is False
    # Individual records exist; an unavailable aggregate does not invent missing calls.
    assert stats['usage_calls'] == stats['priced_calls'] == 2
    assert stats['unknown_usage_calls'] == stats['unknown_cost_calls'] == 0
    json.dumps(stats, allow_nan=False)
