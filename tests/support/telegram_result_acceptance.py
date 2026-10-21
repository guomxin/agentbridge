"""Add the current quarterly inform contract to the existing OA UI fixture.

The fourteen historical synthetic forms remain unchanged. Quarterly uses its
independent strict browser-frame contract fixture, not the generic approval DOM.
"""
from contextlib import contextmanager
from copy import deepcopy

from tests.support.latency_acceptance import TimedPage, TimedWorker
from tests.test_seeyon_quarterly_performance import PerformanceFrame, target


class TelegramOaPage(TimedPage):
    @property
    def frames(self):
        if self.worker.affair_id and self.worker.fixture.get("profile") == "quarterly_performance":
            return [PerformanceFrame(self.worker.fixture["row_snapshot"])]
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
