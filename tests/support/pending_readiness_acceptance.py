"""Real Workspace/Central flow, synthetic OA business-body loading only."""
from contextlib import contextmanager
import time
from unittest.mock import patch

from agentbridge.adapters.seeyon_pending_execution import execute_pending_action
from tests.support.full_business_acceptance import (
    FullBusinessAcceptanceFixture, FullOaWorker, FullOaPage,
)


class ReadinessPage(FullOaPage):
    def __init__(self, worker):
        super().__init__(worker)
        self.ready_started = None
        self.ready_observed = False
        self.dispatch_state = "not_invoked"

    def evaluate(self, script, argument=None):
        if "agentbridge_pending_readiness" in script:
            if self.ready_started is None:
                self.ready_started = time.monotonic()
            elapsed = time.monotonic() - self.ready_started
            fixture = self.worker.fixture
            mode = fixture.get("readiness_case", "late")
            ready = mode != "never" and elapsed >= .3
            if ready and mode in {"identity_drift", "material_drift"}:
                with self.worker.transport.lock:
                    current = self.worker.transport.data["affairs"][self.worker.affair_id]
                    if mode == "identity_drift":
                        current["signals"]["identity"]["process_id"] = "changed-while-body-loaded"
                    else:
                        current["detail"]["fields"][0]["value"] = "合成材料在正文就绪时变化"
                    self.worker.transport.persist()
                fixture = self.worker.fixture
            actual = {**fixture["signals"]["identity"], "affair_id": fixture["source"]["affair_id"],
                      "node_policy": fixture["signals"]["node_policy"],
                      "node_policy_name": fixture["signals"]["node_policy_name"]}
            matches = all(str(actual.get(key) or "") == str(argument[key] or "")
                          for key in actual if key in argument)
            self.ready_observed = ready and matches
            self.worker.transport.data.setdefault("readiness_observations", []).append({
                "affair_id": self.worker.affair_id, "ready": ready, "identity_matches": matches,
                "elapsed_ms": round(elapsed * 1000),
            })
            self.worker.transport.persist()
            return {"identity_matches": matches, "ready": self.ready_observed, "body_loaded": ready,
                    "content_api_loaded": ready, "submit_entry": "submitClickFunc",
                    "reason": "ready" if ready else "body_loading"}
        if "agentbridge_pending_dispatch_observation" in script:
            return {"state": self.dispatch_state}
        if isinstance(argument, dict) and "opinion" in argument:
            if not self.ready_observed:
                raise AssertionError("Synthetic original OA submit entry returned before its body callback")
            result = super().evaluate(script, argument)
            self.dispatch_state = "invoked"
            if self.worker.fixture.get("readiness_case") == "unknown":
                with self.worker.transport.lock:
                    self.worker.transport.data["affairs"][self.worker.affair_id]["processed"] = False
                    self.worker.transport.persist()
            return result
        return super().evaluate(script, argument)

    def wait_for_timeout(self, milliseconds):
        super().wait_for_timeout(milliseconds)
        time.sleep(milliseconds / 1000)


class ReadinessWorker(FullOaWorker):
    def __init__(self, transport, *, is_reader=False):
        super().__init__(transport, is_reader=is_reader)
        self.page = ReadinessPage(self)

    @contextmanager
    def fork_page(self):
        self.readback_opened = True
        try:
            yield ReadinessWorker(self.transport, is_reader=True)
        finally:
            self.readback_closed = True


class PendingReadinessAcceptanceFixture(FullBusinessAcceptanceFixture):
    def __init__(self, home, *, modes=("late", "never", "identity_drift", "material_drift", "unknown")):
        super().__init__(home, seed=False)
        self.service._worker_factories_by_system["oa"] = lambda *_: ReadinessWorker(self.oa)
        def short_execute(*args, **kwargs):
            return execute_pending_action(*args, **{**kwargs, "timeout_seconds": 1})
        self._patches = [patch("agentbridge.adapters.seeyon_pending_execution._READY_TIMEOUT_SECONDS", 1.2),
                         patch("agentbridge.adapters.seeyon_pending_actions.execute_pending_action", short_execute)]
        for item in self._patches:
            item.start()
        for mode in modes:
            if mode not in {"late", "never", "identity_drift", "material_drift", "unknown"}:
                raise ValueError("Unknown isolated readiness scenario")
            case = self.add_pending_case("efficiency_data", case_key="body-" + mode)
            self.oa.data["affairs"][case["arguments"]["affair_id"]]["readiness_case"] = mode
        self.oa.persist()

    def close(self):
        try:
            super().close()
        finally:
            for item in reversed(getattr(self, "_patches", [])):
                item.stop()
