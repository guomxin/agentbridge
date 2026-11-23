import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ADAPTER = Path(__file__).resolve().parents[1] / "integrations/nanobot-agentbridge"
sys.path.insert(0, str(ADAPTER))
from nanobot_agentbridge.runs import RunCoordinator


class NanoBotRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.owner = RunCoordinator(self.directory)
        self.addAsyncCleanup(self.owner.close)
        self.binding = dict(subject="alice", endpoint="web-a", conversation="a-thread")
        self.request = dict(**self.binding, request_key="request-1", payload_digest="digest-a")

    async def test_disconnect_lost_ack_and_restart_preserve_one_run(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(run_id, emit):
            calls.append(run_id)
            emit("公开进度")
            entered.set()
            await release.wait()
            return "公开回答"

        record = self.owner.accept(**self.request, execute=execute)
        await entered.wait()
        recovered = self.owner.accept(**self.request, execute=execute)
        self.assertEqual(record, recovered)
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY"):
            self.owner.accept(**{**self.request, "payload_digest": "different"}, execute=execute)
        with self.assertRaisesRegex(RuntimeError, "already has"):
            RunCoordinator(self.directory)
        for field in self.binding:
            other = {**self.binding, field: "different"}
            with self.assertRaises(KeyError):
                self.owner.events(record["run_id"], **other)
            with self.assertRaises(KeyError):
                await self.owner.cancel(record["run_id"], **other)
        release.set()
        await asyncio.sleep(0)
        await self.owner.close()
        self.owner = RunCoordinator(self.directory)
        self.addAsyncCleanup(self.owner.close)
        result = self.owner.accept(**self.request, execute=execute)
        self.assertEqual(result, {**record, "state": "completed", "answer": "公开回答"})
        events = self.owner.events(result["run_id"], **self.binding)
        self.assertEqual([item["kind"] for item in events], ["accepted", "delta", "completed"])
        self.assertEqual(self.owner.events(result["run_id"], after=events[0]["sequence"],
                                           **self.binding), events[1:])
        self.assertEqual(calls, [record["run_id"]])

    async def test_crash_after_acceptance_never_reinvokes_sdk(self):
        await self.owner.close()
        script = """
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nanobot_agentbridge.runs import RunCoordinator
async def main():
    owner = RunCoordinator(Path(sys.argv[2]))
    async def execute(run_id, emit):
        emit("已收到")
        os._exit(9)
    owner.accept(subject="alice", endpoint="web-a", conversation="a-thread",
                 request_key="request-1", payload_digest="digest-a", execute=execute)
    await asyncio.Event().wait()
asyncio.run(main())
"""
        child = subprocess.run([sys.executable, "-I", "-c", script,
                                str(ADAPTER), str(self.directory)], timeout=10)
        self.assertEqual(child.returncode, 9)
        self.owner = RunCoordinator(self.directory)
        self.addAsyncCleanup(self.owner.close)
        invoked = []
        async def unexpected(run_id, emit):
            invoked.append(run_id)
            return ""
        result = self.owner.accept(**self.request, execute=unexpected)
        self.assertEqual(result["state"], "unknown")
        await asyncio.sleep(0)
        self.assertFalse(invoked)
        self.assertEqual([item["kind"] for item in self.owner.events(result["run_id"], **self.binding)],
                         ["accepted", "delta", "unknown"])

    async def test_unknown_requires_owned_authoritative_commit_before_reconciliation(self):
        async def lost(run_id, emit):
            raise TimeoutError
        run = self.owner.accept(**self.request,execute=lost)["run_id"]
        await asyncio.sleep(.01)
        op = {"operationId":"commit","taskId":"original","userSubject":self.binding["subject"],
              "capability":"oa.leave.save_draft","status":"succeeded","result":{"verification":{"confirmed":True}}}
        for update in ({"taskId":"other"},{"status":"outcome_unknown"},{"result":{}},{"userSubject":"other"}):
            with self.assertRaises(ValueError):
                self.owner.reconcile_verified(run,**self.binding,task_id="original",operation={**op,**update})
        with self.assertRaises(KeyError):
            self.owner.reconcile_verified(run,**{**self.binding,"endpoint":"other"},task_id="original",operation=op)
        for _ in range(2):
            self.assertEqual(self.owner.reconcile_verified(run,**self.binding,task_id="original",operation=op)["state"],"completed")
        self.assertEqual([e["kind"] for e in self.owner.events(run,**self.binding)].count("completed"),1)

    async def test_batch_reconciliation_requires_every_original_item_and_endpoint(self):
        async def lost(run_id, emit):
            raise TimeoutError
        run=self.owner.accept(**self.request,execute=lost)["run_id"]
        await asyncio.sleep(.01)
        task={"taskId":"original","originEndpointId":self.binding["endpoint"],"userSubject":self.binding["subject"]}
        receipt={"taskId":"original","batchId":"batch1","state":"succeeded","allVerified":True,"totalCount":2,"verifiedCount":2}
        def reconcile(t,p):
            return self.owner.reconcile_verified(run,**self.binding,task_id="original",expected_batch_id="batch1",
                batch_snapshot={"task":t,"batchVerification":p})
        for patch in ({"batchId":"other"},{"verifiedCount":1},{"state":"running"},{"allVerified":False}):
            with self.assertRaises(ValueError):
                reconcile(task,{**receipt,**patch})
        with self.assertRaises(ValueError):
            reconcile({**task,"originEndpointId":"other"},receipt)
        self.assertEqual(reconcile(task,receipt)["state"],"completed")

    async def test_explicit_stop_is_bound_to_the_originating_user(self):
        entered = asyncio.Event()
        async def execute(run_id, emit):
            entered.set()
            await asyncio.Event().wait()
        record = self.owner.accept(**self.request, execute=execute)
        await entered.wait()
        stopped = await self.owner.cancel(record["run_id"], **self.binding)
        self.assertEqual(stopped["state"], "stopped")
        self.assertEqual(self.owner.accept(**self.request, execute=execute), stopped)
