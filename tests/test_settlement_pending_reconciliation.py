"""Business-settlement completion retires its unused cards only after verification."""
import pytest

from tests import test_completed_pending_intents as lifecycle
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract
from tests.support.telegram_result_acceptance import add_settlement_affair


@pytest.fixture
def settlement_run(tmp_path, monkeypatch):
    fixture = LatencyAcceptanceFixture(tmp_path, seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    affair = add_settlement_affair(fixture)
    monkeypatch.setattr(lifecycle, "PREPARE", "oa.business_settlement.approval.prepare")
    monkeypatch.setattr(lifecycle, "COMMIT", "oa.business_settlement.approve")
    yield fixture, affair
    fixture.close()


def test_settlement_success_retires_unused_cards_and_preserves_batch(settlement_run):
    fixture, _ = settlement_run
    service = fixture.service
    old = lifecycle.source(settlement_run)
    batch = lifecycle.source(settlement_run, batch=True)
    winner = lifecycle.source(settlement_run)
    result, operation = lifecycle.complete(service, winner)
    assert result["status"] == "succeeded", result
    assert operation["result"]["workflow_profile"] == "business_settlement"
    assert operation["result"]["verification"]["pending_absence_confirmed"] is True
    assert operation["result"]["verification"]["done_identity_confirmed"] is True
    assert service.field_submissions.get(old["input"])["state"] == "superseded"
    assert service.tasks.get_task(old["task"], user_subject=lifecycle.OWNER)["status"] == "superseded"
    lifecycle.pending(service, batch)
    assert len(fixture.oa.writes) == 1
    assert service.resume_interaction(user_subject=lifecycle.OWNER,
        interaction_id=old["interaction"])["error"]["code"] == "INTERACTION_SUPERSEDED"
    assert len(fixture.oa.writes) == 1


def test_settlement_unknown_preserves_unused_card_without_resubmission(settlement_run):
    fixture, _ = settlement_run
    old, winner = lifecycle.source(settlement_run), lifecycle.source(settlement_run)
    fixture.oa.arm("approval_reply_lost")
    result, _ = lifecycle.complete(fixture.service, winner)
    assert result["status"] == "unknown", result
    lifecycle.pending(fixture.service, old)
    assert len(fixture.oa.writes) == 1
