import json
import unittest

from agentbridge.adapters.seeyon_home import (
    extract_history_sections,
    parse_launch_page,
    parse_navigation_inventory,
    parse_oa_detail,
    parse_pending_list,
    parse_pending_projection,
    parse_sent_projection,
    parse_template_center_response,
    parse_template_list,
    parse_template_projection,
)


class SeeyonHomeParserTests(unittest.TestCase):
    def test_parse_oa_detail_extracts_fields_attachments_and_workflow(self):
        html = """
        <html>
          <head><title>Seal request - OA</title></head>
          <body>
            <h1 id="summarySubject">Seal request</h1>
            <table id="formData">
              <tr><th>Applicant</th><td>Alice</td></tr>
              <tr><td>Department</td><td>Finance</td></tr>
            </table>
            <div class="content">Please approve the company seal usage.</div>
            <div id="attachments">
              <a href="/seeyon/fileUpload.do?method=download&fileId=f1">seal-plan.pdf</a>
            </div>
            <table class="processLog">
              <tr><td>Node</td><td>Manager approval</td><td>Opinion: approved</td></tr>
            </table>
          </body>
        </html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary",
        )

        self.assertEqual(result["title"], "Seal request")
        self.assertIn("Please approve", result["text"])
        self.assertEqual(
            result["fields"],
            [
                {"name": "Applicant", "value": "Alice"},
                {"name": "Department", "value": "Finance"},
            ],
        )
        self.assertEqual(result["attachments"][0]["name"], "seal-plan.pdf")
        self.assertEqual(
            result["attachments"][0]["href"],
            "http://10.10.50.110/seeyon/fileUpload.do?method=download&fileId=f1",
        )
        self.assertEqual(result["workflow"][0]["text"], "Node Manager approval Opinion: approved")

    def test_parse_oa_detail_omits_script_and_style_text_from_visible_text(self):
        html = """
        <html>
          <head>
            <title>HR handover - OA</title>
            <style>.secret { color: red; }</style>
            <script>
              var _sessionid = 'SECRET_SESSION_ID';
              var jsonArrBase = '[{"codes":["ContinueSubmit"],"label":"Submit"}]';
            </script>
          </head>
          <body>
            <h1 id="summarySubject">HR handover</h1>
            <div class="content">Please review the handover checklist.</div>
          </body>
        </html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary",
        )

        self.assertIn("Please review the handover checklist.", result["text"])
        self.assertNotIn("_sessionid", result["text"])
        self.assertNotIn("SECRET_SESSION_ID", result["text"])
        self.assertNotIn("jsonArrBase", result["text"])
        self.assertNotIn(".secret", result["text"])
        self.assertEqual(result["actions"][0]["code"], "ContinueSubmit")

    def test_parse_oa_detail_filters_script_like_workflow_noise(self):
        html = """
        <html>
          <body>
            <h1 id="summarySubject">Archive confirmation</h1>
            <div class="workflowAdvanced">
              <!-- var workflowAdvanced = true; //-->
              {{# var prediction = d; }}
              <script>var jsonArrBase = '[{"codes":["Opinion"],"label":"意见"}]';</script>
              function attDivToggle() { return false; }
            </div>
            <div class="processLog">处理人意见区 （共1条，0个赞） 与我相关 （共0条）</div>
            <div class="processLog">处理后归档</div>
            <div class="processLog">流程</div>
            <div class="processLog">Archive confirmation 王玉霄 2026-06-15 15:53 表单 流程 取消 修改流程</div>
            <div class="processLog">王玉霄 已阅 2026-06-15 16:06 回复 ( ) 0</div>
            <div class="processLog">王玉霄 已阅 2026-06-15 16:06 回复 ( ) 0</div>
            <div class="processLog">意见隐藏 不包括: 跟踪 全部 指定人 处理后归档</div>
          </body>
        </html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary",
        )

        self.assertEqual(
            result["workflow"],
            [
                {
                    "text": "王玉霄 已阅 2026-06-15 16:06",
                    "handler": "王玉霄",
                    "opinion": "已阅",
                    "time": "2026-06-15 16:06",
                }
            ],
        )
        self.assertEqual(result["workflow_count"], 1)

    def test_parse_oa_detail_splits_aggregated_workflow_opinions(self):
        html = """
        <html>
          <body>
            <div class="processLog">
              黄佳豪 已阅 2026-06-18 09:51 回复 ( ) 0
              杨宏博 同意 2026-06-18 09:54 回复 ( ) 0
              王玉霄 同意 2026-06-18 10:01 回复 ( ) 0
            </div>
            <div class="processLog">黄佳豪 已阅 2026-06-18 09:51 回复 ( ) 0</div>
          </body>
        </html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary",
        )

        self.assertEqual(
            result["workflow"],
            [
                {
                    "text": "黄佳豪 已阅 2026-06-18 09:51",
                    "handler": "黄佳豪",
                    "opinion": "已阅",
                    "time": "2026-06-18 09:51",
                },
                {
                    "text": "杨宏博 同意 2026-06-18 09:54",
                    "handler": "杨宏博",
                    "opinion": "同意",
                    "time": "2026-06-18 09:54",
                },
                {
                    "text": "王玉霄 同意 2026-06-18 10:01",
                    "handler": "王玉霄",
                    "opinion": "同意",
                    "time": "2026-06-18 10:01",
                },
            ],
        )

    def test_parse_oa_detail_discards_form_ancestors_that_repeat_one_opinion(self):
        html = """
        <html><body>
          <div class="cap4-form">
            乘坐飞机申请单 申请人 郑其荣 出发地点 济南 到达地点 榆林
            审批信息 流程处理意见 【同意】 同意 辛国茂 2026-09-23 14:15
            <div>审批信息 流程处理意见 【同意】 同意 辛国茂 2026-09-23 14:15</div>
            <div>流程处理意见 【同意】 同意 辛国茂 2026-09-23 14:15</div>
          </div>
          <div class="processLog">辛国茂 同意 2026-09-23 14:15 回复 ( ) 0</div>
        </body></html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary",
        )

        self.assertEqual(
            result["workflow"],
            [{
                "text": "辛国茂 同意 2026-09-23 14:15",
                "handler": "辛国茂",
                "opinion": "同意",
                "time": "2026-09-23 14:15",
            }],
        )

    def test_parse_oa_detail_extracts_write_actions_from_page_script(self):
        html = """
        <html>
          <body>
            <h1 id="summarySubject">Contract archive</h1>
            <script>
              var jsonArrBase = '[{"codes":["ContinueSubmit"],"label":"提交","id":"ContinueSubmit"},{"codes":["Opinion"],"label":"意见","id":"Opinion"},{"codes":["Archive"],"label":"处理后归档","id":"Archive"}]';
              var CSRFTOKEN = 'csrf-from-page';
            </script>
            <input type="hidden" name="contentAffairId" value="affair-1">
          </body>
        </html>
        """

        result = parse_oa_detail(html, base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do")

        self.assertEqual(result["action_count"], 3)
        self.assertEqual(
            result["actions"][0],
            {
                "code": "ContinueSubmit",
                "label": "提交",
                "id": "ContinueSubmit",
                "access": "write",
                "risk": "high",
                "requires_confirmation": True,
                "supports_dry_run": True,
                "source": "jsonArrBase",
            },
        )
        self.assertEqual(result["actions"][1]["code"], "Opinion")
        self.assertEqual(result["actions"][1]["risk"], "medium")
        self.assertEqual(
            result["write_hints"],
            {
                "csrf_tokens": [{"name": "CSRFTOKEN", "value_present": True}],
                "hidden_fields": [{"name": "contentAffairId", "value_present": True}],
            },
        )

    def test_parse_oa_detail_extracts_write_endpoint_candidates(self):
        html = """
        <html>
          <body>
            <script>
              function submitOpinion() {
                $.ajax({url:'/seeyon/ajax.do?method=submitOpinion'});
                return '/collaboration/collaboration.do?method=finishWorkItem&from=listPending';
              }
              var readOnlyContent = '/content/content.do?method=index&canDeleteISigntureHtml=true';
            </script>
            <input type="hidden" name="contentAffairId" value="affair-1">
            <input type="hidden" name="summaryId" value="summary-1">
          </body>
        </html>
        """

        result = parse_oa_detail(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do",
        )

        self.assertEqual(
            result["write_hints"]["hidden_fields"],
            [
                {"name": "contentAffairId", "value_present": True},
                {"name": "summaryId", "value_present": True},
            ],
        )
        self.assertEqual(
            result["write_hints"]["endpoint_candidates"],
            [
                {
                    "url": "http://10.10.50.110/seeyon/ajax.do?method=submitOpinion",
                    "method": "UNKNOWN",
                    "risk": "high",
                    "source": "rendered_html",
                    "tested": False,
                },
                {
                    "url": "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=finishWorkItem&from=listPending",
                    "method": "UNKNOWN",
                    "risk": "high",
                    "source": "rendered_html",
                    "tested": False,
                },
            ],
        )

    def test_parse_launch_page_extracts_forms_fields_buttons_and_safe_hints(self):
        html = """
        <html>
          <head>
            <title>Seal launch - OA</title>
            <script>
              var CSRFTOKEN = 'csrf-from-page';
              var jsonArrBase = '[{"codes":["ContinueSubmit"],"label":"Submit","id":"ContinueSubmit"},{"codes":["Archive"],"label":"Archive","id":"Archive"}]';
              function finish() {
                return '/collaboration/collaboration.do?method=finishWorkItem';
              }
            </script>
          </head>
          <body>
            <h1 id="summarySubject">Seal launch</h1>
            <form id="newColl" name="newColl" method="post"
                  action="/collaboration/collaboration.do?method=saveOrUpdate">
              <input type="hidden" name="contentAffairId" value="affair-1">
              <label for="subject">Subject</label>
              <input id="subject" name="subject" type="text" required>
              <textarea id="reason" name="reason" title="Reason"></textarea>
              <select id="sealType" name="sealType">
                <option value="contract">Contract seal</option>
              </select>
              <button id="saveDraft" type="button">Save draft</button>
              <button id="ContinueSubmit" type="button">Submit</button>
            </form>
          </body>
        </html>
        """

        result = parse_launch_page(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&templateId=tpl-1",
        )

        self.assertEqual(result["schema_version"], "agentbridge.oa_launch_inspection.v1")
        self.assertEqual(result["title"], "Seal launch")
        self.assertEqual(result["form_count"], 1)
        self.assertEqual(
            result["forms"][0],
            {
                "index": 0,
                "id": "newColl",
                "name": "newColl",
                "method": "POST",
                "action": "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=saveOrUpdate",
                "field_count": 3,
                "hidden_field_count": 1,
                "button_count": 2,
            },
        )
        self.assertEqual([field["name"] for field in result["fields"]], ["subject", "reason", "sealType"])
        self.assertEqual(result["fields"][0]["label"], "Subject")
        self.assertTrue(result["fields"][0]["required"])
        self.assertEqual(result["fields"][2]["options_count"], 1)
        self.assertEqual(result["hidden_fields"], [{"name": "contentAffairId", "id": "", "value_present": True}])
        buttons = {button["id"]: button for button in result["buttons"]}
        self.assertEqual(buttons["saveDraft"]["risk"], "medium")
        self.assertFalse(buttons["saveDraft"]["action_like"])
        self.assertEqual(buttons["ContinueSubmit"]["risk"], "high")
        self.assertTrue(buttons["ContinueSubmit"]["requires_confirmation"])
        self.assertEqual(result["actions"][0]["code"], "ContinueSubmit")
        self.assertEqual(result["write_hints"]["csrf_tokens"], [{"name": "CSRFTOKEN", "value_present": True}])
        self.assertEqual(result["write_hints"]["endpoint_candidates"][0]["tested"], False)
        self.assertFalse(result["safety"]["execute_allowed"])
        self.assertEqual(result["safety"]["submitted_count"], 0)
        self.assertFalse(result["business_form"]["detected"])

    def test_parse_launch_page_extracts_cap4_business_form_profile(self):
        html = """
        <html>
          <body>
            <div id="cap4Frame">
              cap4 差旅费审批报销单
              流水号 流水号
              发起者信息 姓名 工号 部门
              费用信息 费用归算类型 费用归属部门 结算实体 关联出差申请单
              出差日程及住宿费 日程及住宿起止日期 出差城市 住宿金额 扣除 稽核 电子发票
              在途交通 费用日期 出发地点 到达地点 交通方式 金额 扣除 稽核 电子发票
              异地交通 费用日期 出发地点 到达地点 事由 金额 扣除 稽核 电子发票
              费用统计 住宿费小计 在途交通费小计 异地交通费小计 应付金额合计
              账户信息 账户类型 收款账户 开户银行
            </div>
          </body>
        </html>
        """

        result = parse_launch_page(
            html,
            base_url="http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&templateId=travel-1",
        )

        self.assertEqual(result["fields"], [])
        self.assertEqual(result["field_count"], 0)
        self.assertTrue(result["business_form"]["detected"])
        self.assertEqual(result["business_form"]["source"], "cap4_text")
        self.assertEqual(result["business_form"]["title"], "差旅费审批报销单")
        self.assertIn("费用信息", result["business_form"]["sections"])
        self.assertIn("出差日程及住宿费", result["business_form"]["sections"])
        labels = [candidate["label"] for candidate in result["business_form"]["field_candidates"]]
        self.assertIn("费用归算类型", labels)
        self.assertIn("关联出差申请单", labels)
        self.assertIn("应付金额合计", labels)
        first_label = result["business_form"]["field_candidates"][0]
        self.assertEqual(first_label["source"], "cap4_text")
        self.assertIsNone(first_label["writable"])
        tables = {table["name"]: table for table in result["business_form"]["table_candidates"]}
        self.assertEqual(
            tables["出差日程及住宿费"]["columns"],
            ["日程及住宿起止日期", "出差城市", "住宿金额", "扣除", "稽核", "电子发票"],
        )
        self.assertEqual(
            tables["在途交通"]["columns"],
            ["费用日期", "出发地点", "到达地点", "交通方式", "金额", "扣除", "稽核", "电子发票"],
        )
        self.assertEqual(result["business_form"]["table_count"], 3)

    def test_parse_pending_list_extracts_structured_rows(self):
        html = """
        <div id="section_556815601453123423">
          <table>
            <tr>
              <td>
                <a class="cellContentText" title="Weekly report"
                   onclick="checkAndOpenLink('/collaboration/collaboration.do?method=summary&amp;affairId=abc-123&amp;showTab=true')">
                  <span class="titleText">Weekly report</span>
                </a>
              </td>
              <td>Alice</td>
              <td>Today 08:57</td>
              <td>Collaboration</td>
            </tr>
            <tr class="AlreadyRead">
              <td>
                <a class="cellContentText" title="Contract archive"
                   onclick="checkAndOpenLink('/collaboration/collaboration.do?method=summary&amp;affairId=def-456&amp;showTab=true')">
                  <span class="titleText">Contract archive</span>
                </a>
              </td>
              <td>Bob</td>
              <td>2026-06-15</td>
              <td>Collaboration</td>
            </tr>
          </table>
        </div>
        """

        result = parse_pending_list(html, base_url="http://10.10.50.110/seeyon/main.do?method=main")

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["title"], "Weekly report")
        self.assertEqual(result["items"][0]["sender"], "Alice")
        self.assertEqual(result["items"][0]["date"], "Today 08:57")
        self.assertEqual(result["items"][0]["category"], "Collaboration")
        self.assertEqual(result["items"][0]["affair_id"], "abc-123")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary&affairId=abc-123&showTab=true",
        )
        self.assertFalse(result["items"][0]["read"])
        self.assertTrue(result["items"][1]["read"])

    def test_parse_pending_projection_extracts_rows_from_section_api(self):
        projection = {
            "Name": "全部待办",
            "Data": {
                "dataCount": 1,
                "pageNo": 1,
                "rows": [
                    {
                        "cells": [
                            {
                                "cellContentHTML": "Weekly report",
                                "id": "abc-123",
                                "linkURL": "/collaboration/collaboration.do?method=summary&affairId=abc-123&showTab=true",
                                "className": "ReadDifferFromNotRead",
                            },
                            {"cellContentHTML": "Alice", "alt": "Alice"},
                            {"cellContentHTML": "Today 08:57"},
                            {"cellContentHTML": "Collaboration"},
                        ]
                    }
                ],
            },
        }

        result = parse_pending_projection(
            projection,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["source"], "section_api")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Weekly report")
        self.assertEqual(result["items"][0]["sender"], "Alice")
        self.assertEqual(result["items"][0]["date"], "Today 08:57")
        self.assertEqual(result["items"][0]["category"], "Collaboration")
        self.assertEqual(result["items"][0]["affair_id"], "abc-123")
        self.assertFalse(result["items"][0]["read"])
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary&affairId=abc-123&showTab=true",
        )

    def test_parse_sent_projection_extracts_rows_from_section_api(self):
        projection = {
            "Name": "已发事项",
            "Data": {
                "dataCount": 1,
                "pageNo": 1,
                "rows": [
                    {
                        "cells": [
                            {
                                "cellContentHTML": "Seal request",
                                "id": "sent-123",
                                "linkURL": "/collaboration/collaboration.do?method=summary&openFrom=listSent&affairId=sent-123&showTab=true",
                            },
                            {"cellContentHTML": "已结束"},
                            {"cellContentHTML": "2026-06-15"},
                            {"cellContentHTML": "协同"},
                        ]
                    }
                ],
            },
        }

        result = parse_sent_projection(
            projection,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["source"], "section_api")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Seal request")
        self.assertEqual(result["items"][0]["status"], "已结束")
        self.assertEqual(result["items"][0]["date"], "2026-06-15")
        self.assertEqual(result["items"][0]["category"], "协同")
        self.assertEqual(result["items"][0]["affair_id"], "sent-123")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=summary&openFrom=listSent&affairId=sent-123&showTab=true",
        )

    def test_parse_template_list_extracts_template_ids(self):
        html = """
        <div id="section_-6503951670357636432">
          <table class="chessboardtable" title="Seal request">
            <tr><td class="text_overflow hand"
              onclick="javascript:_openDataLink({'url':'/collaboration/collaboration.do?method=newColl&amp;from=templateNewColl&amp;templateId=-6511139737225050501&amp;showTab=true','obj':this},event)">
              <a>Seal request</a>
            </td></tr>
          </table>
          <table class="chessboardtable" title="Purchase approval">
            <tr><td class="text_overflow hand"
              onclick="javascript:_openDataLink({'url':'/collaboration/collaboration.do?method=newColl&amp;from=templateNewColl&amp;templateId=3492618929488609812&amp;showTab=true','obj':this},event)">
              <a>Purchase approval</a>
            </td></tr>
          </table>
        </div>
        """

        result = parse_template_list(html, base_url="http://10.10.50.110/seeyon/main.do?method=main")

        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["title"], "Seal request")
        self.assertEqual(result["items"][0]["template_id"], "-6511139737225050501")
        self.assertEqual(result["items"][1]["template_id"], "3492618929488609812")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
        )

    def test_parse_sent_projection_uses_cell_content_when_html_is_null(self):
        projection = {
            "Name": "done",
            "Data": {
                "dataNum": 1,
                "pageNo": 0,
                "rows": [
                    {
                        "cells": [
                            {
                                "cellContent": "Historical approval",
                                "cellContentHTML": None,
                                "alt": "Historical approval alt",
                                "linkURL": "/collaboration/collaboration.do?method=summary&affairId=done-1",
                            },
                            {"cellContentHTML": "<span title='Finished'>Finished</span>"},
                            {"cellContentHTML": "<span title='2026-06-20'>2026-06-20</span>"},
                            {"cellContentHTML": "Collaboration"},
                        ]
                    }
                ],
            },
        }

        result = parse_sent_projection(
            projection,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Historical approval")
        self.assertEqual(result["items"][0]["status"], "Finished")
        self.assertEqual(result["items"][0]["affair_id"], "done-1")

    def test_parse_template_projection_extracts_rows_from_section_api(self):
        projection = {
            "Name": "我的模板",
            "Data": {
                "dataCount": 1,
                "pageNo": 1,
                "rows": [
                    {
                        "cells": [
                            {
                                "cellContentHTML": "<span>Seal request</span>",
                                "id": "template-row-1",
                                "linkURL": "/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
                            }
                        ]
                    }
                ],
            },
        }

        result = parse_template_projection(
            projection,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["source"], "section_api")
        self.assertEqual(result["name"], "我的模板")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "Seal request")
        self.assertEqual(result["items"][0]["template_id"], "-6511139737225050501")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
        )

    def test_parse_template_projection_extracts_chessboard_items_from_section_api(self):
        projection = {
            "Name": "我的模板",
            "Data": {
                "dataNum": 1,
                "pageNo": 0,
                "items": [
                    {
                        "title": "【用印】用印申请单",
                        "name": "【用印】用印申请单",
                        "link": "/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
                        "openType": "4",
                    }
                ],
            },
        }

        result = parse_template_projection(
            projection,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["source"], "section_api")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["title"], "【用印】用印申请单")
        self.assertEqual(result["items"][0]["template_id"], "-6511139737225050501")
        self.assertEqual(result["items"][0]["open_type"], "4")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
        )

    def test_parse_template_center_response_extracts_business_metadata(self):
        payload = {
            "code": 0,
            "data": {
                "groups": [
                    {
                        "name": "行政审批",
                        "templates": [
                            {
                                "id": "-6511139737225050501",
                                "subject": "【用印】用印申请单",
                                "formAppId": "2995503502343291344",
                                "categoryName": "行政审批",
                                "moduleType": "1",
                                "bodyType": "20",
                            },
                            {
                                "id": "-7765568933726502821",
                                "subject": "【HR】请假申请单",
                                "formAppId": "6773919591095560889",
                                "categoryName": "人事审批",
                                "moduleType": "1",
                                "bodyType": "20",
                            },
                        ],
                    }
                ]
            },
            "message": "ok",
        }

        result = parse_template_center_response(
            payload,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["source"], "template_center_api")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["items"][0]["title"], "【用印】用印申请单")
        self.assertEqual(result["items"][0]["template_id"], "-6511139737225050501")
        self.assertEqual(result["items"][0]["form_app_id"], "2995503502343291344")
        self.assertEqual(result["items"][0]["category_name"], "行政审批")
        self.assertEqual(result["items"][0]["module_type"], "1")
        self.assertEqual(result["items"][0]["body_type"], "20")
        self.assertEqual(
            result["items"][0]["href"],
            "http://10.10.50.110/seeyon/collaboration/collaboration.do?method=newColl&from=templateNewColl&templateId=-6511139737225050501&showTab=true",
        )

    def test_parse_template_list_accepts_playwright_saved_string_result(self):
        html = json.dumps(
            """
            <div id="section_-6503951670357636432">
              <table class="chessboardtable" title="Seal request">
                <tr><td onclick="javascript:_openDataLink({'url':'/collaboration/collaboration.do?method=newColl&amp;templateId=-6511139737225050501'},event)">
                  <a>Seal request</a>
                </td></tr>
              </table>
            </div>
            """
        )

        result = parse_template_list(html, base_url="http://10.10.50.110/seeyon/main.do?method=main")

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["template_id"], "-6511139737225050501")

    def test_parse_navigation_inventory_extracts_portals_shortcuts_and_sections(self):
        html = """
        <ul id="topNav">
          <li id="spaceLi_5834172664846108460" title="个人空间" class="current"
              onclick="javascript:vPortalMainFrameElements.topCenterNav.showNavigation(0,this)">
            <span>个人空间</span>
          </li>
          <li id="spaceLi_3582122093491471500" title="公司空间"
              onclick="javascript:vPortalMainFrameElements.topCenterNav.showNavigation(1,this)">
            <span>公司空间</span>
          </li>
        </ul>
        <ul id="leftNav">
          <li class="lev1Li">
            <div class="lev1Title navTitleName" title="综合查询"
              onclick="javascript:onSeeyonTopNavMenuClick('/seeyon/isearch.do?method=index','-8434000500218049565','mainfrm','F12_isearch',this)">
              <div class="navText">综合查询</div>
            </div>
          </li>
          <li class="lev1Li">
            <div class="lev1Title navTitleName" title="通讯录"
              onclick="javascript:onSeeyonTopNavMenuClick('/seeyon/addressbook.do?method=homeEntry','-567434236207741830','newWindow','F12_addressbook',this)">
              <div class="navText">通讯录</div>
            </div>
          </li>
        </ul>
        <div id="section_556815601453123423">
          <li id="sectionName_-5754227701614689741" title="全部待办"
              onclick="javascript:changeTabAndReloadSection(&quot;556815601453123423&quot;,&quot;-5754227701614689741&quot;)">
            全部待办(5)
          </li>
          <li id="sectionName_1570596005091691914" title="表单审批">表单审批(4)</li>
        </div>
        """

        result = parse_navigation_inventory(
            html,
            base_url="http://10.10.50.110/seeyon/main.do?method=main",
        )

        self.assertEqual(result["portal_count"], 2)
        self.assertEqual(result["portals"][0]["name"], "个人空间")
        self.assertEqual(result["portals"][0]["portal_id"], "5834172664846108460")
        self.assertTrue(result["portals"][0]["active"])
        self.assertEqual(result["portals"][1]["navigation_index"], "1")
        self.assertEqual(result["shortcut_count"], 2)
        self.assertEqual(result["shortcuts"][0]["name"], "综合查询")
        self.assertEqual(result["shortcuts"][0]["menu_id"], "-8434000500218049565")
        self.assertEqual(result["shortcuts"][0]["target"], "mainfrm")
        self.assertEqual(
            result["shortcuts"][0]["href"],
            "http://10.10.50.110/seeyon/isearch.do?method=index",
        )
        self.assertTrue(result["shortcuts"][1]["opens_new_window"])
        self.assertEqual(result["section_count"], 1)
        self.assertEqual(result["sections"][0]["section_id"], "556815601453123423")
        self.assertEqual(result["sections"][0]["tabs"][0]["name"], "全部待办")
        self.assertEqual(result["sections"][0]["tabs"][0]["count"], 5)

    def test_extract_history_sections_maps_sent_done_and_tracked_tabs(self):
        inventory = {
            "sections": [
                {
                    "section_id": "2760387438530088138",
                    "name": "\u5df2\u53d1\u4e8b\u9879",
                    "tabs": [
                        {
                            "name": "\u5df2\u53d1\u4e8b\u9879",
                            "tab_id": "-1871849768923019549",
                            "active": True,
                            "count": None,
                            "onclick": 'javascript:changeTabAndReloadSection("2760387438530088138","-1871849768923019549")',
                        },
                        {
                            "name": "\u5df2\u529e\u4e8b\u9879",
                            "tab_id": "-5966659239995619621",
                            "active": False,
                            "count": None,
                            "onclick": 'javascript:changeTabAndReloadSection("2760387438530088138","-5966659239995619621")',
                        },
                        {
                            "name": "\u8ddf\u8e2a\u4e8b\u9879",
                            "tab_id": "-9074445271168823642",
                            "active": False,
                            "count": 27,
                            "onclick": 'javascript:changeTabAndReloadSection("2760387438530088138","-9074445271168823642")',
                        },
                    ],
                }
            ]
        }

        result = extract_history_sections(inventory)

        self.assertEqual(result["schema_version"], "agentbridge.oa_history_sections.v1")
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            [(item["kind"], item["tab_id"], item["section_bean_id"]) for item in result["items"]],
            [
                ("sent", "-1871849768923019549", "sentSection"),
                ("done", "-5966659239995619621", "sentSection"),
                ("tracked", "-9074445271168823642", "sentSection"),
            ],
        )
        self.assertTrue(result["items"][0]["active"])
        self.assertEqual(result["items"][2]["count"], 27)


if __name__ == "__main__":
    unittest.main()
