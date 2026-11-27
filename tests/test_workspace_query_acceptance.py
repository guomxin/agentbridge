"""The isolated Chrome fixture exercises all three real central query projections."""

import os
import time
from unittest.mock import patch
from uuid import uuid4

from tests.support.workspace_query_acceptance import Fixture, INSTANCES


def test_three_host_fixture_ordinary_and_shortcut_queries(tmp_path):
    with patch.dict(os.environ):
        fixture = Fixture(tmp_path / "isolated")
        try:
            account = fixture.accounts[fixture.owner]
            for number, (host, instance) in enumerate(INSTANCES.items(), start=1):
                choices = fixture.service.workspace.hosts.choices(account["account_id"])
                changed = fixture.service.workspace.hosts.set_default(account["account_id"], instance,
                                                                       expected_revision=choices["revision"])
                fixture.app.send_chat_stream(account, message="请列出本次合成待办并说明数量",
                    idempotency_key=uuid4().hex, instance_id=instance, expected_revision=changed["revision"])
                fixture.app.query_oa_pending(account, {"requestKey": uuid4().hex,
                    "instanceId": instance, "expectedRevision": changed["revision"], "userMessage": "查询OA待办"})
                deadline = time.monotonic() + 8
                while sum(row["mode"] == "workspace_query_v2" for row in fixture.report()["receipts"]) < number * 2 and time.monotonic() < deadline:
                    time.sleep(.05)
                report = fixture.report()
                assert not report["errors"], report
                queries = [row for row in report["receipts"] if row["mode"] == "workspace_query_v2"]
                assert len(queries) == number * 2, report
                assert sum(row["host"] == host for row in queries) == 2
            assert all(row["acknowledged"] and row["wrongPairDenied"]
                       and row["duplicateBeginDenied"] for row in report["receipts"])
            assert all(row["card"] for row in queries)
            assert all(sum(row["host"] == host and row["publicText"] for row in queries) == 1 for host in INSTANCES)
            texts = [row for row in report["receipts"] if row["mode"] == "workspace_text_v2"]
            assert all(sum(row["host"] == host for row in texts) == 3 for host in INSTANCES)
            assert all(row["publicText"] and not row["card"] for row in texts)
            assert report["realOaCalls"] == report["realTelegramCalls"] == 0
        finally:
            fixture.close()
