from copy import deepcopy
import unittest
from agentbridge.adapters.seeyon_central import SeeyonReadContractMismatch
from agentbridge.adapters.seeyon_history_report import public_history_row, parse_current_nodes, read_report_page
from agentbridge.adapters.seeyon_history_report import _same_route


class HistoryReportEvidenceTests(unittest.TestCase):
    def test_reused_routes_require_the_exact_origin_method_and_affair(self):
        expected="https://oa.example/seeyon/collaboration/collaboration.do?method=summary&affairId=123"
        self.assertTrue(_same_route(expected+"&showTab=true",expected))
        for actual in (expected.replace("https:","http:"),expected.replace("oa.example","elsewhere.example"),
                       expected.replace("summary","listDone"),expected.replace("123","456"),expected+"&affairId=456"):
            with self.subTest(actual=actual):self.assertFalse(_same_route(actual,expected))

    def setUp(self):
        self.source = {"process_id": "123", "case_id": "456"}
        self.snapshot = {
            "process_id": "123", "case_id": "456",
            "process_xml": '<ps><p i="123"><n i="old" t="6" n="旧审批节点"/><n i="now" t="6" n="当前审批节点"/></p></ps>',
            "case_log_xml": '<caseLog><S><F N="old" S="2"/></S><S><F N="old" S="3"/><F N="now" S="2"/></S></caseLog>',
            "workitem_log_xml": '<caseItemLog><WL N="old" PN="原经办人" AS="0,26,5"/><WL N="now" PN="当前处理人" AS="0"/></caseItemLog>',
            "nodes": [{"id": "old", "icons": ["#icon_nodestate_monitor-finish_", "#icon_current"]},
                      {"id": "now", "icons": ["#icon_nodestate_unread_"]}],
        }

    def test_latest_active_node_is_not_selected_historical_personal_node(self):
        self.assertEqual(parse_current_nodes(self.snapshot, self.source),
                         [{"name": "当前审批节点", "handlers": ["当前处理人"]}])

    def test_parallel_nodes_and_multiple_people_are_preserved(self):
        value = deepcopy(self.snapshot)
        value["process_xml"] = value["process_xml"].replace('</p>', '<n i="parallel" t="6" n="并行节点"/></p>')
        value["case_log_xml"] = value["case_log_xml"].replace('</caseLog>', '<S><F N="parallel" S="2"/></S></caseLog>')
        value["workitem_log_xml"] = value["workitem_log_xml"].replace('</caseItemLog>', '<WL N="now" PN="协办人" AS="0,26"/><WL N="parallel" PN="另一人" AS="0"/></caseItemLog>')
        value["nodes"].append({"id":"parallel", "icons":["#icon_nodestate_read_"]})
        self.assertEqual(parse_current_nodes(value, self.source), [
            {"name":"当前审批节点", "handlers":["当前处理人","协办人"]},
            {"name":"并行节点", "handlers":["另一人"]}])

    def test_changed_identity_missing_glyph_and_unknown_action_fail_closed(self):
        variants = [
            {"case_id":"other"}, {"process_id":"other"},
            {"process_xml": self.snapshot["process_xml"].replace('i="123"','i="999"')},
            {"nodes":[{"id":"now","icons":["#icon_current"]}]},
            {"nodes":[{"id":"now","icons":[]},{"id":"now","icons":[]}]},
            {"workitem_log_xml":self.snapshot["workitem_log_xml"].replace('AS="0"','AS="0,99"')},
            {"case_log_xml":'<caseLog><S><F N="missing" S="2"/></S></caseLog>'},
            {"process_xml":self.snapshot["process_xml"].replace('i="now" t="6"','i="now" t="99"')},
            {"process_xml":'<!DOCTYPE ps [<!ENTITY x "bad">]><ps>&x;</ps>'},
        ]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(SeeyonReadContractMismatch):
                parse_current_nodes({**self.snapshot,**changes}, self.source)

    def test_dates_remain_distinct_and_node_name_is_not_used_as_current(self):
        row = public_history_row({"affairId":"123","subject":"业务结算单", "startDate":"2026-10-01 10:20",
            "dealTime":"2026-11-05 11:30", "nodeName":"历史节点", "affairNodeName":"历史节点",
            "flowFinished":False,"currentNodesInfo":"处理人甲、处理人乙"}, "done")
        self.assertEqual(row["initiated_at"],"2026-10-01T10:20:00+08:00")
        self.assertEqual(row["processed_at"],"2026-11-05T11:30:00+08:00")
        self.assertIsNone(row["current_nodes"])
        self.assertEqual(row["current_handlers"],["处理人甲","处理人乙"])
        ended = public_history_row({"affairId":"123","subject":"已结束事项","flowFinished":True},"done")
        self.assertEqual(ended["current_nodes"],[])
        self.assertEqual(ended["workflow_state"],"ended")
        self.assertIn("initiated_at",ended["unavailable_fields"])

    def test_invalid_affair_and_date_never_get_coerced(self):
        for identity in (123, "123?method=submit", "", None):
            with self.subTest(identity=identity), self.assertRaises(SeeyonReadContractMismatch):
                public_history_row({"affairId":identity,"subject":"x"},"done")
        row = public_history_row({"affairId":"123","subject":"x","startDate":"2026-02-30 10:00"},"done")
        self.assertIsNone(row["initiated_at"])
