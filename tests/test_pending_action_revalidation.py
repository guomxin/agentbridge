"""A freshness read must never navigate the active OA submission document."""
from unittest.mock import Mock

import pytest

from tests.support.full_business_acceptance import FullBusinessAcceptanceFixture, FullOaWorker


@pytest.mark.parametrize("cached_scope", [False, True])
def test_current_material_revalidation_keeps_action_page_when_route_discovery_can_navigate(tmp_path, cached_scope):
    fixture = FullBusinessAcceptanceFixture(tmp_path, seed=False)
    try:
        case = fixture.add_pending_case("efficiency_data")
        worker = FullOaWorker(fixture.oa)
        adapter = fixture.service.adapter
        affair = case["arguments"]["affair_id"]
        with adapter.capability_scope(worker):
            source, initial = adapter.resolve_workflow_detail(worker, collection="pending", affair_id=affair)
            action_url = worker.page_url
            action_page = worker.page
            worker.goto = Mock(side_effect=AssertionError("Action page must not navigate during revalidation"))
            if cached_scope:
                current_source, current = adapter.revalidate_workflow_detail(worker, collection="pending", affair_id=affair)
                assert not worker.readback_opened
        if not cached_scope:
            current_source, current = adapter.revalidate_workflow_detail(worker, collection="pending", affair_id=affair)
            assert worker.readback_opened and worker.readback_closed
        assert worker.page is action_page
        assert worker.page_url == action_url
        worker.goto.assert_not_called()
        assert current_source["affair_id"] == source["affair_id"]
        assert current["_trusted_materials"] == initial["_trusted_materials"]
    finally:
        fixture.close()
