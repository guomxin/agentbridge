import unittest

from agentbridge.adapters.seeyon_pending_actions import (
    PendingActionContractMismatch,
    acknowledge_weekly_report,
    approve_business_trip_request,
    approve_efficiency_data,
    approve_intellectual_property_declaration,
    approve_labor_contract_renewal,
    approve_leave_request,
    approve_overtime,
    approve_resignation,
    confirm_attendance,
    pending_action_contract_fingerprint,
    preflight_pending_action,
    prepare_attendance_confirmation,
    prepare_business_trip_approval,
    prepare_efficiency_data_approval,
    prepare_intellectual_property_declaration_approval,
    prepare_labor_contract_renewal_approval,
    prepare_leave_approval,
    prepare_overtime_approval,
    prepare_resignation_approval,
    prepare_standard_collaboration_approval,
    prepare_travel_expense_approval,
    prepare_weekly_report_acknowledgement,
)


class PendingActionTests(unittest.TestCase):
    def test_prepare_efficiency_data_binds_exact_pending_item(self):
        worker = FakeWorker(_fixture("efficiency_data"))
        prepared = prepare_efficiency_data_approval(
            FakeAdapter(worker), worker, _inputs()
        )

        self.assertEqual(prepared["plan"]["business_intent"], "approve_efficiency_data")
        self.assertEqual(prepared["plan"]["target"]["affair_id"], "affair-1")
        self.assertEqual(prepared["plan"]["target"]["summary_id"], "summary-1")
        self.assertNotIn("detail", prepared["plan"])
        self.assertEqual(prepared["summary"]["authorize_label"], "授权审批通过")

    def test_prepare_travel_expense_requires_registered_template_and_form(self):
        fixture = _fixture("travel_expense")
        worker = FakeWorker(fixture)
        prepared = prepare_travel_expense_approval(
            FakeAdapter(worker), worker, _inputs()
        )
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["应付金额合计"], "303.00")
        self.assertEqual(fields["附件数量"], "1")
        self.assertNotIn("收款账号", fields)

        changed = _fixture("travel_expense")
        changed["signals"]["identity"]["form_app_id"] = "changed-form"
        changed_worker = FakeWorker(changed)
        with self.assertRaisesRegex(PendingActionContractMismatch, "form identity"):
            prepare_travel_expense_approval(
                FakeAdapter(changed_worker), changed_worker, _inputs()
            )

    def test_labor_contract_renewal_freezes_selected_business_values(self):
        fixture = _fixture("labor_contract_renewal")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        prepared = prepare_labor_contract_renewal_approval(
            adapter, worker, _inputs()
        )
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }

        self.assertEqual(fields["综合评价"], "优秀，完全胜任岗位工作")
        self.assertEqual(fields["续签建议"], "续签劳动合同")
        self.assertTrue(
            prepared["plan"]["target"]["business_snapshot"][
                "business_fields_browse_only"
            ]
        )

        result = approve_labor_contract_renewal(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "labor_contract_renewal")

    def test_labor_contract_business_change_blocks_before_boundary(self):
        fixture = _fixture("labor_contract_renewal")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_labor_contract_renewal_approval(
            adapter, worker, _inputs()
        )["plan"]
        fixture["business_snapshot"]["renewal_recommendation"] = "终止劳动合同"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "business_snapshot"):
            approve_labor_contract_renewal(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_intellectual_property_declaration_freezes_read_only_business_values(self):
        fixture = _fixture("intellectual_property_declaration")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_intellectual_property_declaration_approval(
            adapter, worker, _inputs()
        )
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }

        self.assertEqual(fields["知识产权类型"], "软件著作权")
        self.assertEqual(fields["申报名称"], "综合管理工作台1.0")
        self.assertEqual(fields["权属类型"], "集团独有")
        self.assertEqual(fields["申请材料"], "软著-综合管理工作台.rar (14M)")
        self.assertTrue(
            prepared["plan"]["target"]["business_snapshot"][
                "business_fields_browse_only"
            ]
        )

        result = approve_intellectual_property_declaration(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(
            result["workflow_profile"], "intellectual_property_declaration"
        )

    def test_intellectual_property_business_change_blocks_before_boundary(self):
        fixture = _fixture("intellectual_property_declaration")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_intellectual_property_declaration_approval(
            adapter, worker, _inputs()
        )["plan"]
        fixture["business_snapshot"]["declaration_name"] = "changed"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "business_snapshot"):
            approve_intellectual_property_declaration(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_weekly_report_is_acknowledgement_not_approval(self):
        worker = FakeWorker(_fixture("weekly_report"))
        prepared = prepare_weekly_report_acknowledgement(
            FakeAdapter(worker), worker, _inputs(opinion="已阅")
        )
        contract = prepared["plan"]["action_contract"]
        self.assertEqual(contract["action_kind"], "acknowledgement")
        self.assertEqual(prepared["summary"]["authorize_label"], "授权阅办周报")

    def test_attendance_confirmation_freezes_oa_decision_and_confirms(self):
        fixture = _fixture("attendance_confirmation")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_attendance_confirmation(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["OA 当前确认结论"], "无异议")
        self.assertEqual(
            prepared["plan"]["action_contract"]["action_kind"],
            "confirmation",
        )

        result = confirm_attendance(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )

        self.assertTrue(result["workflow_confirmed"])
        self.assertEqual(result["workflow_profile"], "attendance_confirmation")
        self.assertEqual(worker.page.commit_payload["attitude_code"], "agree")

    def test_overtime_approval_freezes_exact_hr_contract_and_confirms(self):
        fixture = _fixture("overtime")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_overtime_approval(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertIn("郑其荣", fields["姓名"])
        self.assertIn("2026-08-17 18:30", fields["申请开始时间"])
        self.assertIn("0.65", fields["实际开始时间"])
        self.assertEqual(
            prepared["plan"]["target"]["template_id"],
            "-170938662527873499",
        )

        result = approve_overtime(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "overtime")

    def test_leave_approval_freezes_read_only_hr_fields_and_confirms(self):
        fixture = _fixture("leave")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_leave_approval(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["申请人"], "韩志平")
        self.assertEqual(fields["请假类型"], "年休")
        self.assertEqual(fields["开始时间"], "2026-09-28 08:30")
        self.assertEqual(fields["请假天数"], "3.00")
        self.assertEqual(fields["事由"], "回老家探亲。")
        self.assertTrue(
            prepared["plan"]["target"]["business_snapshot"][
                "business_fields_browse_only"
            ]
        )

        result = approve_leave_request(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "leave")

    def test_leave_business_change_blocks_before_boundary(self):
        fixture = _fixture("leave")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_leave_approval(adapter, worker, _inputs())["plan"]
        fixture["business_snapshot"]["leave_days"] = "4.00"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "business_snapshot"):
            approve_leave_request(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_business_trip_approval_freezes_read_only_hr_fields_and_confirms(self):
        fixture = _fixture("business_trip")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_business_trip_approval(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["申请人"], "郑其荣")
        self.assertEqual(fields["出差工具"], "飞机")
        self.assertEqual(fields["出差目的地"], "北京")
        self.assertEqual(fields["出差天数"], "2.00")
        self.assertEqual(fields["直接上级"], "马述杰")
        self.assertTrue(
            prepared["plan"]["target"]["business_snapshot"][
                "business_fields_browse_only"
            ]
        )

        result = approve_business_trip_request(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "business_trip")

    def test_business_trip_change_blocks_before_boundary(self):
        fixture = _fixture("business_trip")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_business_trip_approval(adapter, worker, _inputs())["plan"]
        fixture["business_snapshot"]["destination"] = "上海"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "business_snapshot"):
            approve_business_trip_request(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_resignation_approval_freezes_read_only_hr_fields_and_confirms(self):
        fixture = _fixture("resignation")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_resignation_approval(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["员工"], "杨芮杰")
        self.assertEqual(fields["申请离职时间"], "2026-08-31")
        self.assertEqual(fields["是否有证书"], "否")
        self.assertEqual(
            prepared["plan"]["target"]["business_snapshot"]["certificate_name"],
            "",
        )
        self.assertEqual(fields["手写离职申请"], "离职申请.jpg (283KB)")
        self.assertTrue(
            prepared["plan"]["target"]["business_snapshot"][
                "business_fields_browse_only"
            ]
        )

        result = approve_resignation(
            adapter,
            worker,
            prepared["plan"],
            enter_commit_boundary=lambda: None,
        )
        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "resignation")

    def test_resignation_with_certificate_freezes_certificate_name(self):
        fixture = _fixture("resignation")
        fixture["business_snapshot"]["has_certificate"] = "是"
        fixture["business_snapshot"]["certificate_name"] = "软件设计师证书"
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        prepared = prepare_resignation_approval(adapter, worker, _inputs())
        fields = {
            item["label"]: item["value"] for item in prepared["summary"]["fields"]
        }
        self.assertEqual(fields["是否有证书"], "是")
        self.assertEqual(fields["证书名称"], "软件设计师证书")

    def test_resignation_with_certificate_but_no_name_fails_closed(self):
        fixture = _fixture("resignation")
        fixture["business_snapshot"]["has_certificate"] = "是"
        fixture["business_snapshot"]["certificate_name"] = ""
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)

        with self.assertRaisesRegex(
            PendingActionContractMismatch,
            "certificate name",
        ):
            prepare_resignation_approval(adapter, worker, _inputs())

    def test_resignation_business_change_blocks_before_boundary(self):
        fixture = _fixture("resignation")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_resignation_approval(adapter, worker, _inputs())["plan"]
        fixture["business_snapshot"]["resignation_date"] = "2026-09-01"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "business_snapshot"):
            approve_resignation(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_pending_preflight_rejects_wrong_profile_before_field_input(self):
        worker = FakeWorker(_fixture("attendance_confirmation"))
        with self.assertRaisesRegex(PendingActionContractMismatch, "not a registered"):
            preflight_pending_action(
                FakeAdapter(worker),
                worker,
                {"affair_id": "affair-1"},
                "standard_collaboration",
            )

    def test_standard_collaboration_rejects_specialist_titles_and_fields(self):
        for body_type in (None, "", "20", "FORM"):
            unknown = _fixture("standard_collaboration")
            unknown["signals"]["identity"]["body_type"] = body_type
            worker = FakeWorker(unknown)
            with self.assertRaises(PendingActionContractMismatch):
                prepare_standard_collaboration_approval(FakeAdapter(worker), worker, _inputs())
        specialist = _fixture("standard_collaboration")
        specialist["source"]["title"] = "【报销】其他报销单-Alice"
        worker = FakeWorker(specialist)
        with self.assertRaisesRegex(PendingActionContractMismatch, "not a registered"):
            prepare_standard_collaboration_approval(
                FakeAdapter(worker), worker, _inputs()
            )

        extra_field = _fixture("standard_collaboration")
        extra_field["detail"]["fields"].append({"name": "金额", "value": "10"})
        worker = FakeWorker(extra_field)
        with self.assertRaisesRegex(PendingActionContractMismatch, "outside"):
            prepare_standard_collaboration_approval(
                FakeAdapter(worker), worker, _inputs()
            )

    def test_commit_acknowledges_and_verifies_pending_disappearance(self):
        fixture = _fixture("weekly_report")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_weekly_report_acknowledgement(
            adapter, worker, _inputs(opinion="已阅")
        )["plan"]
        boundary = []

        result = acknowledge_weekly_report(
            adapter,
            worker,
            plan,
            enter_commit_boundary=lambda: boundary.append("consumed"),
        )

        self.assertEqual(boundary, ["consumed"])
        self.assertTrue(result["workflow_acknowledged"])
        self.assertTrue(result["verification"]["confirmed"])
        self.assertEqual(worker.page.commit_payload["action_kind"], "acknowledgement")

    def test_commit_approval_sets_approval_result(self):
        fixture = _fixture("efficiency_data")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_efficiency_data_approval(adapter, worker, _inputs())["plan"]

        result = approve_efficiency_data(
            adapter,
            worker,
            plan,
            enter_commit_boundary=lambda: None,
        )

        self.assertTrue(result["workflow_approved"])
        self.assertEqual(result["workflow_profile"], "efficiency_data")

    def test_commit_verifies_on_forked_page_after_submission(self):
        fixture = _fixture("overtime")
        worker = FakeForkingWorker(fixture)
        adapter = FakeForkingAdapter(worker)
        plan = prepare_overtime_approval(adapter, worker, _inputs())["plan"]

        result = approve_overtime(
            adapter,
            worker,
            plan,
            enter_commit_boundary=lambda: None,
        )

        self.assertTrue(result["workflow_approved"])
        self.assertIs(adapter.readback_worker, worker.readback_worker)
        self.assertTrue(worker.readback_closed)

    def test_commit_rejects_changed_detail_before_boundary(self):
        fixture = _fixture("efficiency_data")
        worker = FakeWorker(fixture)
        adapter = FakeAdapter(worker)
        plan = prepare_efficiency_data_approval(adapter, worker, _inputs())["plan"]
        fixture["detail"]["fields"][0]["value"] = "changed"
        boundary = []

        with self.assertRaisesRegex(PendingActionContractMismatch, "detail_fingerprint"):
            approve_efficiency_data(
                adapter,
                worker,
                plan,
                enter_commit_boundary=lambda: boundary.append("consumed"),
            )
        self.assertEqual(boundary, [])

    def test_contract_fingerprints_are_profile_specific(self):
        fingerprints = {
            pending_action_contract_fingerprint(profile)
            for profile in (
                "efficiency_data",
                "travel_expense",
                "business_trip",
                "labor_contract_renewal",
                "intellectual_property_declaration",
                "overtime",
                "leave",
                "resignation",
                "work_handover",
                "flight_application",
                "attendance_confirmation",
                "weekly_report",
                "standard_collaboration",
            )
        }
        self.assertEqual(len(fingerprints), 13)


class FakeAdapter:
    def __init__(self, worker):
        self.worker = worker
        self.pending_reads = 0

    def resolve_workflow_detail(self, worker, *, collection, affair_id):
        self.assert_worker(worker)
        if collection != "pending" or affair_id != "affair-1":
            raise AssertionError("unexpected pending target")
        return self.worker.fixture["source"], self.worker.fixture["detail"]

    def list_workflows(self, worker, *, collection, arguments):
        self.assert_worker(worker)
        if collection != "pending" or arguments != {"limit": 100}:
            raise AssertionError("unexpected pending readback")
        self.pending_reads += 1
        return {"items": []}

    def assert_worker(self, worker):
        if worker is not self.worker:
            raise AssertionError("unexpected worker")


class FakeWorker:
    def __init__(self, fixture):
        self.fixture = fixture
        self.page = FakePage(fixture)


class FakeForkingAdapter(FakeAdapter):
    def __init__(self, worker):
        super().__init__(worker)
        self.readback_worker = None

    def list_workflows(self, worker, *, collection, arguments):
        if worker is not self.worker.readback_worker:
            raise AssertionError("pending readback did not use the forked worker")
        if collection != "pending" or arguments != {"limit": 100}:
            raise AssertionError("unexpected pending readback")
        self.readback_worker = worker
        return {"items": []}


class FakeForkingWorker(FakeWorker):
    def __init__(self, fixture):
        super().__init__(fixture)
        self.readback_worker = object()
        self.readback_closed = False

    def fork_page(self):
        return FakeForkedWorkerContext(self)


class FakeForkedWorkerContext:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self.owner.readback_worker

    def __exit__(self, _exc_type, _exc, _traceback):
        self.owner.readback_closed = True


class FakePage:
    def __init__(self, fixture):
        self.fixture = fixture
        self.commit_payload = None
        self.frames = (
            [FakeBusinessFrame(fixture)] if fixture.get("business_snapshot") else []
        )

    def evaluate(self, _script, argument):
        if isinstance(argument, str):
            return self.fixture["signals"]
        self.commit_payload = dict(argument)
        return {"scheduled": True, "submit_entry": "submitClickFunc"}

    def on(self, _event, _callback):
        return None


class FakeBusinessFrame:
    url = "http://oa.example.test/seeyon/common/cap4/form/index.html"

    def __init__(self, fixture):
        self.fixture = fixture

    def locator(self, selector):
        expected_by_profile = {
            "attendance_confirmation": "#field0097_id",
            "business_trip": "#field0027_id",
            "intellectual_property_declaration": "#field0010_id",
            "leave": "#field0023_id",
            "resignation": "#field0006_id",
        }
        expected = expected_by_profile.get(
            self.fixture.get("profile"), "#field0008_id"
        )
        if selector != expected:
            raise AssertionError("unexpected business-form selector")
        return FakeCountLocator()

    def evaluate(self, _script):
        return dict(self.fixture["business_snapshot"])


class FakeCountLocator:
    @staticmethod
    def count():
        return 1


def _inputs(**changes):
    return {"affair_id": "affair-1", "opinion": "同意", **changes}


def _fixture(profile):
    fixtures = {
        "efficiency_data": {
            "title": "2026年第29周人工智能研发中心效能数据",
            "fields": [{"name": "接收人", "value": "Alice"}],
            "template_id": "",
            "form_app_id": "",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
        },
        "travel_expense": {
            "title": "【报销】差旅费审批报销单-Alice-303.00",
            "fields": [
                {"name": "流水号", "value": "20260722001"},
                {"name": "姓名", "value": "Alice"},
                {"name": "费用归算类型", "value": "部门"},
                {"name": "费用归属部门", "value": "研发中心"},
                {"name": "费用归属事项", "value": "项目"},
                {"name": "关联出差申请单", "value": "出差申请"},
                {"name": "应付金额合计", "value": "303.00"},
                {"name": "收款账号", "value": "6222000000000000"},
            ],
            "template_id": "-2046021869351779722",
            "form_app_id": "-2571419096251022663",
            "node_policy": "报销审批",
            "node_policy_name": "报销审批",
            "attitudes": ["agree", "disagree"],
        },
        "labor_contract_renewal": {
            "title": "(自动发起)【HR】劳动合同续签表-Alice",
            "fields": [
                {"name": "姓名", "value": "Alice 工号 A001 所属部门 研发中心 岗位 工程师"},
                {
                    "name": "入职日期",
                    "value": "2023-09-06 合同续签开始日期 2026-09-06 合同续签结束日期 2029-09-05",
                },
                {"name": "综合评价及指导意见", "value": "继续保持"},
                {"name": "综合评价", "value": "优秀 良好 基本满足"},
                {"name": "鉴于以上意见，建议", "value": "续签劳动合同 终止劳动合同"},
                {"name": "续签情况反馈", "value": "已签订劳动合同并领取本人留存份"},
            ],
            "template_id": "3868679303223263344",
            "form_app_id": "6514522401641018463",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "guidance": "创新能力强，研发能力强，继续保持。",
                "evaluation": "优秀，完全胜任岗位工作",
                "renewal_recommendation": "续签劳动合同",
            },
        },
        "intellectual_property_declaration": {
            "title": "【知识产权】知识产权申报审批单-软件著作权",
            "fields": [
                {"name": "申请人", "value": "孙冯林 TH2104 人工智能研发中心"},
                {"name": "知识产权类型", "value": "软件著作权"},
                {"name": "研发项目名称", "value": ""},
                {"name": "名称", "value": "综合管理工作台1.0"},
                {"name": "申报名称", "value": ""},
                {"name": "发明/设计人", "value": "郑其荣、侯建民、孙冯林"},
                {"name": "权属类型", "value": "集团独有"},
                {"name": "权属单位", "value": "泰华智慧产业集团股份有限公司"},
                {"name": "其他权属", "value": ""},
                {"name": "申请用途", "value": "其他"},
                {"name": "著作权名称", "value": ""},
                {"name": "申请材料", "value": "软著-综合管理工作台.rar (14M)"},
            ],
            "template_id": "-2986710992990286032",
            "form_app_id": "-6972097064001584076",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "applicant": "孙冯林",
                "employee_number": "TH2104",
                "department": "人工智能研发中心",
                "application_date": "2026-08-06",
                "intellectual_property_type": "软件著作权",
                "declaration_name": "综合管理工作台1.0",
                "inventors": "郑其荣、侯建民、孙冯林",
                "ownership_type": "集团独有",
                "ownership_unit": "泰华智慧产业集团股份有限公司",
                "application_purpose": "其他",
                "other_purpose": "保护创新",
                "application_material": "软著-综合管理工作台.rar (14M)",
            },
        },
        "weekly_report": {
            "title": "(自动发起)【综合】周报发送流程-研发中心-28周",
            "fields": [
                {"name": "周报名称", "value": "第28周"},
                {"name": "年度", "value": "2026"},
                {"name": "本周说明", "value": "正常"},
                {"name": "本周 工作总结", "value": "总结"},
                {"name": "下周 工作计划", "value": "计划"},
            ],
            "template_id": "1610567580409022440",
            "form_app_id": "-2351708227632217917",
            "node_policy": "inform",
            "node_policy_name": "知会",
            "attitudes": [],
        },
        "attendance_confirmation": {
            "title": "(自动发起)【HR】月度考勤确认单-Alice-2026年7月",
            "fields": [
                {
                    "name": "姓名",
                    "value": "Alice 部门 研发中心 年度 2026 月度 7",
                },
                {
                    "name": "应出勤天数",
                    "value": "23.00 实际出勤天数 23.00 缺勤天数 0.00",
                },
                {
                    "name": "本月考勤是否有异议",
                    "value": "是否有异议 有异议 无异议 异议说明",
                },
            ],
            "template_id": "-7231800401165464345",
            "form_app_id": "5072944770639779741",
            "node_policy": "sendoredit",
            "node_policy_name": "发起人填写",
            "attitudes": ["haveRead", "agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "selection_count": 1,
                "decision": "无异议",
            },
        },
        "overtime": {
            "title": "【HR】加班申请审核单-郑其荣-0.65",
            "fields": [
                {
                    "name": "姓名",
                    "value": "申请人 郑其荣 工号 TH809 部门 人工智能研发中心",
                },
                {
                    "name": "申请开始时间",
                    "value": "申请加班开始时间 2026-08-17 18:30 申请加班结束时间 2026-08-17 19:09",
                },
                {"name": "加班事由", "value": "日常加班"},
                {
                    "name": "是否存在有非部门经理的直接上级（组负责人）",
                    "value": "是否有直接上级 否",
                },
                {
                    "name": "实际开始时间",
                    "value": "实际加班开始时间 2026-08-17 18:30 实际加班结束时间 2026-08-17 19:09 加班时长 0.65",
                },
            ],
            "template_id": "-170938662527873499",
            "form_app_id": "-213895278619572887",
            "node_policy": "考勤审批",
            "node_policy_name": "考勤审批",
            "attitudes": ["agree", "disagree"],
        },
        "leave": {
            "title": "【HR】请假申请单-韩志平-年休",
            "fields": [
                {
                    "name": "姓名",
                    "value": "韩志平 TH1193 人工智能研发中心 发起日期 2026-09-20",
                },
                {
                    "name": "请假类型",
                    "value": "年休 剩余年休 5.88天 剩余调休 61.41小时",
                },
                {
                    "name": "开始时间",
                    "value": "2026-09-28 08:30 结束时间 2026-09-30 17:30 3.00天 22.50小时",
                },
                {"name": "事由", "value": "回老家探亲。"},
                {"name": "证明材料", "value": ""},
                {
                    "name": "是否存在有非部门经理的直接上级（组负责人）",
                    "value": "否",
                },
            ],
            "template_id": "-7765568933726502821",
            "form_app_id": "6773919591095560889",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "applicant": "韩志平",
                "employee_number": "TH1193",
                "department": "人工智能研发中心",
                "application_date": "2026-09-20",
                "leave_type": "年休",
                "remaining_annual_leave_days": "5.88",
                "remaining_compensatory_leave_hours": "61.41",
                "start_time": "2026-09-28 08:30",
                "end_time": "2026-09-30 17:30",
                "leave_days": "3.00",
                "leave_hours": "22.50",
                "reason": "回老家探亲。",
                "has_direct_supervisor": "否",
            },
        },
        "business_trip": {
            "title": "【HR】出差申请单-郑其荣-2026-09-22 05:35-2026-09-23 17:35",
            "fields": [
                {
                    "name": "姓名",
                    "value": "郑其荣 TH809 人工智能研发中心 发起日期 2026-09-21",
                },
                {
                    "name": "出差开始时间",
                    "value": "2026-09-22 05:35 出差结束时间 2026-09-23 17:35 出差工具 飞机",
                },
                {
                    "name": "出差始发地",
                    "value": "济南 出差目的地 北京 出差天数 2.00 出差小时数 15.00",
                },
                {"name": "事由", "value": "项目沟通"},
                {
                    "name": "是否存在有非部门经理的直接上级（组负责人）",
                    "value": "是 直接上级 马述杰",
                },
            ],
            "template_id": "2668910351205287097",
            "form_app_id": "4948077657800057670",
            "node_policy": "approve",
            "node_policy_name": "直接上级",
            "attitudes": ["agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "applicant": "郑其荣",
                "employee_number": "TH809",
                "department": "人工智能研发中心",
                "application_date": "2026-09-21",
                "start_time": "2026-09-22 05:35",
                "end_time": "2026-09-23 17:35",
                "travel_mode": "飞机",
                "origin": "济南",
                "destination": "北京",
                "trip_days": "2.00",
                "trip_hours": "15.00",
                "reason": "项目沟通",
                "has_direct_supervisor": "是",
                "direct_supervisor": "马述杰",
            },
        },
        "resignation": {
            "title": "【HR】离职申请单-杨芮杰-人工智能研发中心-实习生",
            "fields": [
                {
                    "name": "姓名",
                    "value": "申请人 杨芮杰 工号 TH2352 所属部门 人工智能研发中心 岗位 实习生",
                },
                {
                    "name": "入职时间",
                    "value": "入职时间 2026-07-09 申请离职时间 2026-08-31 岗位类别 研发",
                },
                {"name": "是否有证书", "value": "是否有证书 是 否"},
                {"name": "离职原因", "value": "实习结束，返校"},
                {
                    "name": "手写离职 申请附件",
                    "value": "离职申请.jpg (283KB)",
                },
            ],
            "template_id": "3483439346772952417",
            "form_app_id": "9167110384557358951",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
            "business_snapshot": {
                "browse_only": True,
                "employee": "杨芮杰",
                "employee_number": "TH2352",
                "department": "人工智能研发中心",
                "position": "实习生",
                "hire_date": "2026-07-09",
                "resignation_date": "2026-08-31",
                "position_category": "研发",
                "has_certificate": "否",
                "certificate_name": "",
                "resignation_reason": "实习结束，返校",
                "handwritten_application": "离职申请.jpg (283KB)",
            },
        },
        "standard_collaboration": {
            "title": "关于征集专家入库工作的通知",
            "fields": [{"name": "接收人", "value": "Alice"}],
            "template_id": "",
            "form_app_id": "",
            "node_policy": "approve",
            "node_policy_name": "审批",
            "attitudes": ["agree", "disagree"],
        },
    }
    selected = fixtures[profile]
    return {
        "profile": profile,
        "business_snapshot": selected.get("business_snapshot"),
        "source": {
            "affair_id": "affair-1",
            "title": selected["title"],
            "sender": "Sender",
            "date": "2026-07-22",
        },
        "detail": {
            "title": selected["title"],
            "fields": selected["fields"],
            "attachments": (
                [{"name": "receipt.pdf"}] if profile == "travel_expense" else []
            ),
            "workflow": [{"opinion": "submitted"}],
            "actions": [{"code": "ContinueSubmit"}],
        },
        "signals": {
            "affair_matches": True,
            "comment_present": True,
            "submit_present": True,
            "page_path": "/seeyon/collaboration/collaboration.do",
            "node_policy": selected["node_policy"],
            "node_policy_name": selected["node_policy_name"],
            "attitude_codes": selected["attitudes"],
            "identity": {
                "summary_id": "summary-1",
                "process_id": "process-1",
                "body_type": "20" if selected["form_app_id"] else "10",
                "template_id": selected["template_id"],
                "form_app_id": selected["form_app_id"],
                "form_record_id": "record-1" if selected["form_app_id"] else "",
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
