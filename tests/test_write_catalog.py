from agentbridge.core import write_catalog


def test_every_registered_write_has_callable_bindings_and_scope_policy():
    for prepare, definition in write_catalog._TRUSTED_WRITE_DEFINITIONS.items():
        assert write_catalog.capability_required_scopes(prepare)
        assert write_catalog.capability_required_scopes(definition['commit_capability'])
        for key in ('prepare_function', 'commit_function', 'field_schema_function', 'preflight_function'):
            if key in definition:
                assert callable(write_catalog.resolve_write_function(definition[key])), (prepare, key)
    assert write_catalog.resolve_write_function('__import__') is None
    assert write_catalog.resolve_write_function('capability_required_scopes') is None
