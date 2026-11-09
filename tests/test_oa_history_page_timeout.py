from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from agentbridge.core.oa_history_queries import OaHistoryQueryStore
from agentbridge.core.oa_history_query_service import OaHistoryQueryService


class HistoryPageDeadlineTests(unittest.TestCase):
    def test_browser_deadline_preserves_cursor_and_unfinished_page(self):
        with TemporaryDirectory(prefix="oa-history-page-timeout-") as folder:
            tick = [time.monotonic() + 1000]
            calls = []
            def reader(_adapter, _worker, *, collection, page_number, temporal):
                calls.append(page_number)
                if len(calls) == 1:
                    tick[0] += 21
                    raise RuntimeError("synthetic browser evaluator deadline")
                return {"total":0,"pages":0,"size":50,"page":page_number,"items":[]}
            service = OaHistoryQueryService(OaHistoryQueryStore(Path(folder)/"state.db"),
                read_page=reader, monotonic=lambda:tick[0])
            binding = dict(user_subject="alice",session_id="session",principal_ref="principal",adapter=None,worker=None)
            first = service.query(**binding, collection="done",temporal={"kind":"all"},accepted_at="2026-11-09T09:00:00+08:00")
            self.assertEqual(first["state"],"scanning")
            self.assertEqual(first["progress"]["scanned_count"],0)
            self.assertTrue(first["has_more"])
            final = service.next(**binding,cursor=first["next_cursor"])
            self.assertEqual(final["state"],"complete")
            self.assertFalse(final["has_more"])
            self.assertTrue(final["coverage"]["scan_complete"])
            self.assertEqual(calls,[1,1,1])

    def test_browser_error_before_deadline_stays_explicit_partial(self):
        with TemporaryDirectory(prefix="oa-history-page-error-") as folder:
            def reader(*args,**kwargs):raise RuntimeError("synthetic unavailable source")
            service = OaHistoryQueryService(OaHistoryQueryStore(Path(folder)/"state.db"),read_page=reader)
            result=service.query(user_subject="alice",session_id="session",principal_ref="principal",
                adapter=None,worker=None,collection="done",temporal={"kind":"all"},accepted_at="2026-11-09T09:00:00+08:00")
            self.assertEqual(result["state"],"partial")
            self.assertEqual(result["coverage"]["completionReason"],"source_read_failed")
            self.assertFalse(result["coverage"]["scan_complete"])
