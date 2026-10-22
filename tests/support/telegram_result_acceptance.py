"""Add the current quarterly and reward inform contracts to the OA UI fixture.

The fourteen historical synthetic forms remain unchanged. Inform workflows use
their independent strict browser-frame contracts.
"""
from contextlib import contextmanager
from copy import deepcopy

from tests.support.latency_acceptance import TimedPage, TimedWorker
from tests.test_seeyon_quarterly_performance import PerformanceFrame, target
from tests.test_seeyon_intellectual_property_reward import RewardFrame, target as reward_target
from tests.test_seeyon_business_settlement import SettlementFrame, target as settlement_target


class TelegramOaPage(TimedPage):
    @property
    def frames(self):
        if self.worker.affair_id and self.worker.fixture.get("profile") == "quarterly_performance":
            return [PerformanceFrame(self.worker.fixture["row_snapshot"])]
        if self.worker.affair_id and self.worker.fixture.get("profile") == "intellectual_property_reward":
            return [RewardFrame(self.worker.fixture["row_snapshot"])]
        if self.worker.affair_id and self.worker.fixture.get("profile") == "business_settlement":
            return [SettlementFrame(self.worker.fixture["row_snapshot"])]
        return super().frames


class TelegramOaWorker(TimedWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = TelegramOaPage(self)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        self.transport.latency.io("readbackOpen")
        try:
            yield TelegramOaWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


def add_quarterly_affair(fixture, affair_id="synthetic-tg-quarterly-performance"):
    source, snapshot, _, _ = target.__wrapped__()
    source = deepcopy(source)
    source.update(profile="quarterly_performance", row_snapshot=snapshot, processed=False,
        body_text="合成季度绩效原始材料；保留员工评级，只进行知会阅办。")
    source["source"].update(affair_id=affair_id, sender="合成申请人", date="2026-10-20")
    source["detail"]["title"] = source["source"]["title"]
    source["detail"].setdefault("attachments", [])
    source["signals"]["identity"].update(summary_id="summary-" + affair_id,
        process_id="process-" + affair_id, form_record_id="record-" + affair_id)
    fixture.oa.data["affairs"][affair_id] = source
    fixture.oa.persist()
    fixture.service._worker_factories_by_system["oa"] = lambda *_: TelegramOaWorker(fixture.oa)
    return affair_id


def add_reward_affair(fixture, affair_id="synthetic-tg-intellectual-property-reward"):
    source, snapshot, _, _ = reward_target.__wrapped__()
    source = deepcopy(source)
    source.update(profile="intellectual_property_reward", row_snapshot=snapshot, processed=False,
        body_text="合成知识产权奖励原始材料；保留奖励金额和奖励人员，仅进行知会阅办。")
    source["source"].update(affair_id=affair_id, sender="合成申请人", date="2026-10-21")
    source["detail"]["title"] = source["source"]["title"]
    source["detail"].setdefault("attachments", [])
    source["signals"]["identity"].update(summary_id="summary-" + affair_id,
        process_id="process-" + affair_id, form_record_id="record-" + affair_id)
    fixture.oa.data["affairs"][affair_id] = source
    fixture.oa.persist()
    fixture.service._worker_factories_by_system["oa"] = lambda *_: TelegramOaWorker(fixture.oa)
    return affair_id


def add_settlement_affair(fixture, affair_id="synthetic-tg-business-settlement"):
    source, snapshot, _, _ = settlement_target.__wrapped__()
    source = deepcopy(source)
    source.update(profile="business_settlement", row_snapshot=snapshot, processed=False,
        body_text="合成业务结算单原始材料；核对只读收款账户、费用明细和应付金额后审批。")
    source["source"].update(affair_id=affair_id, sender="合成申请人", date="2026-10-22")
    source["detail"]["title"] = source["source"]["title"]
    source["detail"].setdefault("attachments", [])
    source["signals"]["identity"].update(summary_id="summary-" + affair_id,
        process_id="process-" + affair_id, form_record_id="record-" + affair_id)
    fixture.oa.data["affairs"][affair_id] = source
    fixture.oa.persist()
    fixture.service._worker_factories_by_system["oa"] = lambda *_: TelegramOaWorker(fixture.oa)
    return affair_id
