import unittest

from agentbridge.core.transforms import (
    MERGE_WORK_ITEMS,
    WORK_ITEMS_TO_LOG_DRAFT,
    WORK_ITEMS_TO_LOG_DRAFT_V2,
    TransformRejected,
    build_transform_registry,
)


class TaskPlanTransformTests(unittest.TestCase):
    def setUp(self):
        self.registry = build_transform_registry()

    def test_work_items_are_deduplicated_and_automatic_items_are_excluded(self):
        result = self.registry.invoke(
            WORK_ITEMS_TO_LOG_DRAFT,
            {
                "items": [
                    {
                        "affair_id": "1",
                        "title": "出差申请",
                        "date": "2026-08-30 09:00",
                        "category": "审批",
                    },
                    {
                        "affair_id": "1",
                        "title": "出差申请",
                        "date": "2026-08-30 09:00",
                        "category": "审批",
                    },
                    {
                        "affair_id": "2",
                        "title": "（自动发起）周报发送流程",
                        "date": "2026-08-30 10:00",
                        "category": "通知",
                    },
                ]
            },
        )

        self.assertFalse(result["empty"])
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["included_count"], 1)
        self.assertEqual(result["excluded_duplicate_count"], 1)
        self.assertEqual(result["excluded_automatic_count"], 1)
        self.assertIn("出差申请", result["draft"])
        self.assertNotIn("周报发送流程", result["draft"])

    def test_empty_source_produces_explicit_empty_result(self):
        result = self.registry.invoke(WORK_ITEMS_TO_LOG_DRAFT, {"items": []})

        self.assertTrue(result["empty"])
        self.assertEqual(result["draft"], "")
        self.assertEqual(result["included_count"], 0)

    def test_input_limit_is_enforced(self):
        with self.assertRaises(TransformRejected) as raised:
            self.registry.invoke(
                WORK_ITEMS_TO_LOG_DRAFT,
                {"items": [{"title": str(index)} for index in range(101)]},
            )

        self.assertEqual(raised.exception.code, "TRANSFORM_INPUT_TOO_LARGE")

    def test_multi_source_merge_preserves_done_and_sent_business_actions(self):
        done = self.source(
            "done",
            [{"affair_id": "same", "title": "项目申请", "date": "2026-08-30"}],
        )
        sent = self.source(
            "sent",
            [{"affair_id": "same", "title": "项目申请", "date": "2026-08-30"}],
        )

        merged = self.registry.invoke(MERGE_WORK_ITEMS, {"sources": [done, sent]})
        draft = self.registry.invoke(
            WORK_ITEMS_TO_LOG_DRAFT_V2,
            {"bundle": merged},
        )

        self.assertEqual(merged["item_count"], 2)
        self.assertEqual(merged["duplicate_count"], 0)
        self.assertIn("处理《项目申请》", draft["draft"])
        self.assertIn("发起《项目申请》", draft["draft"])
        self.assertFalse(draft["source_incomplete"])

    def test_incomplete_source_is_propagated_without_becoming_complete(self):
        done = self.source("done", [], status="complete")
        sent = self.source("sent", [], status="partial", has_more=True)

        merged = self.registry.invoke(MERGE_WORK_ITEMS, {"sources": [done, sent]})

        self.assertEqual(merged["coverage"]["status"], "partial")
        self.assertTrue(merged["coverage"]["hasMore"])

    def test_merge_rejects_more_than_two_hundred_business_items(self):
        source = self.source(
            "done",
            [
                {"affair_id": str(index), "title": f"事项 {index}", "date": "2026-08-30"}
                for index in range(201)
            ],
        )

        with self.assertRaises(TransformRejected) as raised:
            self.registry.invoke(MERGE_WORK_ITEMS, {"sources": [source]})

        self.assertEqual(raised.exception.code, "TRANSFORM_INPUT_TOO_LARGE")

    def test_merge_preserves_server_query_evidence_for_each_source(self):
        source = self.source("done", [{"title": "审批", "affair_id": "1", "date": "2026-08-30"}])
        source["coverage"].update({
            "serverFilterApplied": True, "sourceQueryTotal": 1,
            "sourceQueryPages": 1, "pagesFetched": 1, "scanBudget": 1000,
        })
        merged = self.registry.invoke(MERGE_WORK_ITEMS, {"sources": [source]})
        self.assertEqual(merged["source_summaries"][0]["coverage"], source["coverage"])

    @staticmethod
    def source(collection, items, *, status="complete", has_more=False):
        return {
            "collection": collection,
            "items": [
                {
                    "status": "",
                    "category": "",
                    **item,
                }
                for item in items
            ],
            "coverage": {
                "status": status,
                "queryApplied": True,
                "dateBasis": "processed_at" if collection == "done" else "initiated_at",
                "requestedRange": {"start": "2026-08-30", "end": "2026-08-30"},
                "scannedCount": len(items),
                "matchedCount": len(items),
                "hasMore": has_more,
                "completionReason": "test",
                "observedAt": "2026-08-31T09:00:00+08:00",
                "queryHash": f"sha256:{collection}",
            },
        }


if __name__ == "__main__":
    unittest.main()
