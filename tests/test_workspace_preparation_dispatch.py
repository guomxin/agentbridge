from types import SimpleNamespace
import threading
import unittest

from agentbridge.workspace.application import WorkspaceApplication


class PreparationDispatchTests(unittest.TestCase):
    def test_more_than_four_preparations_drain_without_waiting_for_runtime_recovery(self):
        release = threading.Event()
        condition = threading.Condition()
        calls = []
        active = 0
        peak = 0

        class Gateway:
            def call(self, method, params, **kwargs):
                nonlocal active, peak
                with condition:
                    calls.append((method, params))
                    active += 1
                    peak = max(peak, active)
                    condition.notify_all()
                release.wait(2)
                with condition:
                    active -= 1
                    condition.notify_all()
                if params["taskId"] == "task-0":
                    raise RuntimeError("Synthetic lost wake response")
                return {"status": "accepted"}

        store = SimpleNamespace(recover_host_dispatches=lambda: [], issue_gateway_grant=lambda account: {
            "session_key": "synthetic-session", "endpoint_key": "synthetic-endpoint", "grant": "synthetic-grant"})
        service = SimpleNamespace(workspace=store, runtime_governance=None,
            skill_authoring=SimpleNamespace(workbench=SimpleNamespace(close=lambda: None)))
        app = WorkspaceApplication(service=service, gateway=Gateway())
        self.addCleanup(app.close)
        self.addCleanup(release.set)
        with app._dispatch_workers_lock:
            for index in range(9):
                app._preparation_queue[f"task-{index}"] = ({"account_id": "synthetic-account"},
                    {"taskId": f"task-{index}", "commandKey": f"command-{index}"})
            app._start_preparation_workers_locked()
        with condition:
            self.assertTrue(condition.wait_for(lambda: len(calls) == 4, timeout=2))
        self.assertEqual(peak, 4)
        release.set()
        with condition:
            self.assertTrue(condition.wait_for(lambda: len(calls) == 9 and active == 0, timeout=2))
        self.assertLessEqual(peak, 4)
        self.assertEqual(len({params["taskId"] for _, params in calls}), 9)
        self.assertTrue(all(method == "agentbridge.task.wake" for method, _ in calls))
        self.assertTrue(all(set(params) == {"sessionKey", "endpointKey", "grant", "taskId", "commandKey"}
                            for _, params in calls))


if __name__ == "__main__":
    unittest.main()
