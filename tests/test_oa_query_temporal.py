import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agentbridge.core.oa_query_temporal import (
    DATE_BASES, TEMPORAL_KINDS, normalize_oa_query_temporal,
    oa_query_temporal_schema, validate_oa_query_temporal,
)
from agentbridge.core.planning_policy import compile_temporal_constraints
from agentbridge.core.task_plan_validation import (
    PlanValidationError, TaskPlanTemporalConstraintInput, _normalize_constraints,
)


class OAQueryTemporalTests(unittest.TestCase):
    def normalize(self, temporal, *, accepted_at="2026-11-08T17:30:00Z", **options):
        return normalize_oa_query_temporal(
            temporal, accepted_at=accepted_at, default_date_basis="processed_at", **options,
        )

    def test_explicit_closed_and_open_ranges_have_distinct_meanings(self):
        cases = [
            ({"kind": "between", "start": "2026-10-01", "end": "2026-10-31"}, ("2026-10-01", "2026-10-31")),
            ({"kind": "absolute_range", "start": "2026-10-01", "end": "2026-10-31"}, ("2026-10-01", "2026-10-31")),
            ({"kind": "since", "start": "2026-10-01"}, ("2026-10-01", "2026-11-09")),
            ({"kind": "until", "end": "2026-10-31"}, (None, "2026-10-31")),
            ({"kind": "all"}, (None, None)),
        ]
        for temporal, (start, end) in cases:
            with self.subTest(kind=temporal["kind"]):
                result = self.normalize(temporal)
                self.assertEqual(result["absoluteRange"], {"start": start, "end": end})
                self.assertEqual(result["constraint"], temporal)
                self.assertEqual((result["precision"], result["bounds"]), ("day", "inclusive"))

    def test_relative_ranges_use_the_local_request_day(self):
        cases = [
            ({"kind": "previous_day"}, ("2026-11-08", "2026-11-08")),
            ({"kind": "day_before_yesterday"}, ("2026-11-07", "2026-11-07")),
            ({"kind": "previous_calendar_week"}, ("2026-11-02", "2026-11-08")),
            ({"kind": "previous_calendar_month"}, ("2026-10-01", "2026-10-31")),
            ({"kind": "current_calendar_month"}, ("2026-11-01", "2026-11-09")),
            ({"kind": "rolling_days", "days": 7}, ("2026-11-03", "2026-11-09")),
            ({"kind": "rolling_days", "days": 1}, ("2026-11-09", "2026-11-09")),
        ]
        for temporal, (start, end) in cases:
            with self.subTest(temporal=temporal):
                result = self.normalize(temporal)
                self.assertEqual(result["absoluteRange"], {"start": start, "end": end})
                self.assertEqual(result["timeZone"], "Asia/Shanghai")
                self.assertEqual(result["acceptedAt"], "2026-11-08T17:30:00+00:00")

    def test_calendar_boundaries_include_leap_day_and_previous_year(self):
        for anchor, expected in [
            ("2024-03-01T00:00:00+08:00", {"start": "2024-02-01", "end": "2024-02-29"}),
            ("2026-01-01T00:00:00+08:00", {"start": "2025-12-01", "end": "2025-12-31"}),
        ]:
            with self.subTest(anchor=anchor):
                self.assertEqual(self.normalize({"kind": "previous_calendar_month"}, accepted_at=anchor)["absoluteRange"], expected)

    def test_date_basis_is_selected_explicitly_and_never_substituted(self):
        result = self.normalize({"kind": "since", "start": "2026-10-01", "date_basis": "initiated_at"},
                                supported_date_bases=list(DATE_BASES))
        self.assertEqual(result["dateBasis"], "initiated_at")
        with self.assertRaisesRegex(ValueError, "不支持日期口径 initiated_at"):
            self.normalize({"kind": "until", "end": "2026-10-31", "date_basis": "initiated_at"},
                           supported_date_bases=["processed_at"])

    def test_rejects_ambiguous_shapes_precision_and_invalid_ranges(self):
        invalid = [
            {"kind": "between", "start": "2026-10-01"},
            {"kind": "absolute_range", "end": "2026-10-01"},
            {"kind": "between", "start": "2026-10-02", "end": "2026-10-01"},
            {"kind": "between", "start": "2026-02-29", "end": "2026-03-01"},
            {"kind": "since"}, {"kind": "until"},
            {"kind": "since", "start": "2026-10-01", "end": "2026-10-02"},
            {"kind": "until", "start": "2026-10-01", "end": "2026-10-02"},
            {"kind": "all", "start": "2000-01-01"},
            {"kind": "previous_day", "end": "2026-10-01"},
            {"kind": "rolling_days", "days": True},
            {"kind": "rolling_days", "days": 0},
            {"kind": "rolling_days", "days": 3661},
            {"kind": "rolling_days", "days": "7"},
            {"kind": "rolling_days"},
            {"kind": "all", "days": 7},
            {"kind": "since", "start": "2026-10-01T10:30:00"},
            {"kind": "since", "start": "2026-10-1"},
            {"kind": "since", "start": " 2026-10-01"},
            {"kind": "since", "start": 20261001},
            {"kind": "all", "date_basis": "created_at"},
            {"kind": "all", "accepted_at": "2020-01-01T00:00:00Z"},
            {"kind": "all", "as_of": "2026-10-01"},
            {"kind": ["all"]}, None,
        ]
        for temporal in invalid:
            with self.subTest(temporal=temporal), self.assertRaises(ValueError):
                self.normalize(temporal)

    def test_since_cannot_start_after_the_request_day(self):
        with self.assertRaisesRegex(ValueError, "不能晚于请求日"):
            self.normalize({"kind": "since", "start": "2026-11-10"})

    def test_request_timestamp_and_timezone_are_required_and_trusted(self):
        for accepted_at in (None, "", "invalid", "2026-11-09", "2026-11-09T10:00:00"):
            with self.subTest(accepted_at=accepted_at), self.assertRaises(ValueError):
                self.normalize({"kind": "all"}, accepted_at=accepted_at)
        with self.assertRaisesRegex(ValueError, "时区"):
            self.normalize({"kind": "previous_day"}, timezone_name="Unknown/Zone")

    def test_json_schema_and_plan_typed_schema_expose_the_same_kinds(self):
        public = oa_query_temporal_schema()["properties"]
        typed = TaskPlanTemporalConstraintInput.model_json_schema()["properties"]
        self.assertEqual(public["kind"]["enum"], list(TEMPORAL_KINDS))
        self.assertEqual(typed["kind"]["enum"], list(TEMPORAL_KINDS))
        self.assertEqual(TaskPlanTemporalConstraintInput(kind="since", start="2026-10-01", date_basis="initiated_at").date_basis,
                         "initiated_at")
        with self.assertRaises(ValidationError):
            TaskPlanTemporalConstraintInput(kind="rolling_days", days=True)

    def test_manual_plan_validation_uses_the_same_shape_rules(self):
        valid = {"kind": "rolling_days", "days": 14, "date_basis": "processed_at"}
        self.assertEqual(_normalize_constraints({"temporal": valid}, proposal_version="agentbridge.task-plan.proposal.v2"),
                         {"temporal": valid})
        with self.assertRaises(PlanValidationError) as raised:
            _normalize_constraints({"temporal": {"kind": "since"}}, proposal_version="agentbridge.task-plan.proposal.v2")
        self.assertEqual(raised.exception.code, "PLAN_SCHEMA_INVALID")
        self.assertEqual(validate_oa_query_temporal({"kind": "all", "start": None, "end": None}), {"kind": "all"})


class OAPlanTemporalCompilationTests(unittest.TestCase):
    def proposal(self, temporal, *, capability="oa.workflow.history.query", collection="done", arguments=None):
        return {
            "schemaVersion": "agentbridge.task-plan.proposal.v2", "goal": "只读查询",
            "constraints": {"temporal": temporal},
            "steps": [{"stepKey": "read", "kind": "capability", "capabilityName": capability,
                       "arguments": {**({"collection": collection} if capability.endswith(".query") else {}), **(arguments or {})}}],
        }

    def compile(self, proposal):
        return compile_temporal_constraints(proposal, accepted_at="2026-11-09T23:59:59+08:00")

    def test_new_query_freezes_since_before_login_or_pagination(self):
        temporal = {"kind": "since", "start": "2026-10-01", "date_basis": "initiated_at"}
        proposal = self.proposal(temporal)
        compiled, context = self.compile(proposal)
        frozen = compiled["steps"][0]["arguments"]["temporal"]
        self.assertEqual(frozen, {"kind": "between", "start": "2026-10-01", "end": "2026-11-09", "date_basis": "initiated_at"})
        resumed = normalize_oa_query_temporal(frozen, accepted_at="2026-11-11T00:01:00+08:00")
        self.assertEqual(resumed["absoluteRange"], context["absoluteRange"])
        self.assertEqual(context["constraint"], temporal)
        self.assertEqual(context["stepDateBasis"], {"read": "initiated_at"})
        self.assertNotIn("temporal", proposal["steps"][0]["arguments"])

    def test_all_is_supported_only_by_a_declared_all_history_source(self):
        compiled, context = self.compile(self.proposal({"kind": "all"}))
        self.assertEqual(compiled["steps"][0]["arguments"]["temporal"], {"kind": "all", "date_basis": "processed_at"})
        self.assertEqual(context["absoluteRange"], {"start": None, "end": None})
        for capability in ("oa.workflow.done.list", "oa.workflow.sent.list"):
            with self.subTest(capability=capability), self.assertRaisesRegex(ValueError, "不支持全部历史"):
                self.compile(self.proposal({"kind": "all"}, capability=capability))

    def test_legacy_open_upper_range_does_not_invent_lower_boundary(self):
        compiled, context = self.compile(self.proposal({"kind": "until", "end": "2026-10-31"}, capability="oa.workflow.done.list"))
        self.assertEqual(compiled["steps"][0]["arguments"], {"end_date": "2026-10-31"})
        self.assertEqual(context["absoluteRange"], {"start": None, "end": "2026-10-31"})

    def test_legacy_since_uses_request_day_and_own_verified_date_basis(self):
        for capability, basis in (("oa.workflow.done.list", "processed_at"), ("oa.workflow.sent.list", "initiated_at")):
            with self.subTest(capability=capability):
                compiled, context = self.compile(self.proposal({"kind": "since", "start": "2026-10-01"}, capability=capability))
                self.assertEqual(compiled["steps"][0]["arguments"], {"start_date": "2026-10-01", "end_date": "2026-11-09"})
                self.assertEqual(context["stepDateBasis"], {"read": basis})
        with self.assertRaisesRegex(ValueError, "不支持日期口径 initiated_at"):
            self.compile(self.proposal({"kind": "since", "start": "2026-10-01", "date_basis": "initiated_at"},
                                      capability="oa.workflow.done.list"))

    def test_new_query_default_basis_depends_on_collection(self):
        compiled, context = self.compile(self.proposal({"kind": "all"}, collection="sent"))
        self.assertEqual(compiled["steps"][0]["arguments"]["temporal"]["date_basis"], "initiated_at")
        self.assertEqual(context["stepDateBasis"], {"read": "initiated_at"})
        with self.assertRaisesRegex(ValueError, "不支持日期口径 processed_at"):
            self.compile(self.proposal({"kind": "since", "start": "2026-10-01", "date_basis": "processed_at"}, collection="sent"))
        for collection in (None, "tracked", {}, ["done"]):
            with self.subTest(collection=collection), self.assertRaisesRegex(ValueError, "collection"):
                self.compile(self.proposal({"kind": "all"}, collection=collection))

    def test_explicit_step_range_and_basis_must_match_plan(self):
        temporal = {"kind": "since", "start": "2026-10-01"}
        for step_temporal in ({"kind": "since", "start": "2026-10-02"},
                              {"kind": "since", "start": "2026-10-01", "date_basis": "initiated_at"}):
            with self.subTest(step_temporal=step_temporal), self.assertRaisesRegex(ValueError, "与时间约束不一致"):
                self.compile(self.proposal(temporal, arguments={"temporal": step_temporal}))
        compiled, _ = self.compile(self.proposal(temporal, arguments={"temporal": temporal}))
        self.assertEqual(compiled["steps"][0]["arguments"]["temporal"]["end"], "2026-11-09")

    def test_unregistered_temporal_mapping_cannot_silently_claim_all_support(self):
        descriptor = {"sourceContract": {"temporalArgument": "temporal", "dateBasis": "processed_at"}}
        with patch("agentbridge.core.planning_policy.planning_descriptor", return_value=descriptor):
            with self.assertRaisesRegex(ValueError, "不支持全部历史"):
                self.compile(self.proposal({"kind": "all"}))

    def test_old_v1_plan_is_not_recompiled(self):
        proposal = {"schemaVersion": "agentbridge.task-plan.proposal.v1", "steps": []}
        compiled, context = self.compile(proposal)
        self.assertEqual(compiled, proposal)
        self.assertIsNone(context)


if __name__ == "__main__":
    unittest.main()
