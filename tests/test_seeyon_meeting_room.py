import json
from urllib.parse import parse_qs
import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_meeting_room import (
    MeetingRoomContractMismatch,
    list_meeting_room_availability,
    list_my_meeting_room_applications,
    query_my_meeting_room_applications,
    response_json,
    resolve_room,
)


class SeeyonMeetingRoomTests(unittest.TestCase):
    def test_null_response_is_only_allowed_for_void_write_acknowledgements(self):
        response = {"status": 200, "url": "https://oa.example/seeyon/ajax.do", "json": None, "text": "null"}
        with self.assertRaises(MeetingRoomContractMismatch):
            response_json(response, context="getMyApps")
        self.assertEqual(response_json(response, context="cancelRoomApp", allow_empty_success=True), {})
        with self.assertRaises(MeetingRoomContractMismatch):
            response_json({**response, "status": 500}, context="cancelRoomApp", allow_empty_success=True)

    def test_raw_application_completeness_rejects_truncation_duplicates_and_total_changes(self):
        row1, row2 = {"roomAppId": "1"}, {"roomAppId": "2"}
        cases = [
            ([{"data": [], "total": 0, "pages": 0}], 20, True),
            ([{"data": [row1, row2], "total": 2, "pages": 1}], 1, False),
            ([{"data": [row1, row1], "total": 2, "pages": 1}], 20, False),
            ([{"data": [row1], "total": 1, "pages": 1}], 20, True),
            ([{"data": [row1], "total": 3, "pages": 2},
              {"data": [row2], "total": 2, "pages": 2}], 20, False),
        ]
        for payloads, maximum, expected in cases:
            with self.subTest(payloads=payloads, maximum=maximum), patch(
                "agentbridge.adapters.seeyon_meeting_room.meeting_ajax", side_effect=payloads,
            ):
                result = query_my_meeting_room_applications(None, None, maximum_items=maximum)
                self.assertEqual(result["complete"], expected)

    def test_availability_returns_booking_owner_without_meeting_details(self):
        worker = FakeMeetingRoomWorker()

        result = list_meeting_room_availability(
            FakeAdapter(),
            worker,
            {
                "start_time": "2026-07-20 14:00",
                "end_time": "2026-07-20 16:00",
            },
        )

        self.assertTrue(result["availability_evaluated"])
        self.assertEqual(result["summary"]["available_count"], 1)
        self.assertEqual(result["summary"]["occupied_count"], 1)
        occupied = next(room for room in result["rooms"] if room["room_id"] == "room-3")
        self.assertFalse(occupied["available"])
        self.assertEqual(occupied["capacity"], 18)
        self.assertFalse(occupied["requires_approval"])
        self.assertEqual(occupied["approval_requirement_code"], "2")
        self.assertEqual(occupied["approval_requirement_label"], "无需审批")
        self.assertEqual(occupied["busy_intervals"][0]["status_label"], "已审核")
        self.assertEqual(
            occupied["busy_intervals"][0]["booked_by_name"],
            "张三",
        )
        self.assertEqual(
            occupied["busy_intervals"][0]["booked_by_department"],
            "研发中心",
        )
        self.assertNotIn("meeting_name", occupied["busy_intervals"][0])
        self.assertNotIn("person-secret", json.dumps(result, ensure_ascii=False))
        self.assertEqual(worker.methods, ["roomListInfo"])
        self.assertEqual(
            worker.arguments[0],
            [{
                "roomName": "",
                "sortType": "-1",
                "startDatetime": 1784527200000,
                "endDatetime": 1784534400000,
            }],
        )

    def test_availability_filters_free_rooms_and_capacity(self):
        result = list_meeting_room_availability(
            FakeAdapter(),
            FakeMeetingRoomWorker(),
            {
                "start_time": "2026-07-20 14:00",
                "end_time": "2026-07-20 16:00",
                "minimum_capacity": 20,
                "only_available": True,
            },
        )

        self.assertEqual([room["room_name"] for room in result["rooms"]], ["4层2#会议室"])

    def test_room_directory_without_time_does_not_claim_availability(self):
        result = list_meeting_room_availability(
            FakeAdapter(),
            FakeMeetingRoomWorker(),
            {"room_name": "会议室"},
        )

        self.assertFalse(result["availability_evaluated"])
        self.assertTrue(all(room["available"] is None for room in result["rooms"]))
        self.assertTrue(all(not room["busy_intervals"] for room in result["rooms"]))

    def test_room_approval_requirement_uses_oa_three_state_enum(self):
        result = list_meeting_room_availability(
            FakeAdapter(),
            FakeMeetingRoomWorker(),
            {},
        )

        room_2 = next(room for room in result["rooms"] if room["room_id"] == "room-2")
        room_3 = next(room for room in result["rooms"] if room["room_id"] == "room-3")
        self.assertTrue(room_2["requires_approval"])
        self.assertEqual(room_2["approval_requirement_code"], "1")
        self.assertFalse(room_3["requires_approval"])
        self.assertEqual(room_3["approval_requirement_code"], "2")

    def test_room_name_alias_is_resolved_against_full_directory(self):
        worker = FakeMeetingRoomWorker()

        result = list_meeting_room_availability(
            FakeAdapter(),
            worker,
            {"room_name": "三号会议室"},
        )

        self.assertEqual([room["room_name"] for room in result["rooms"]], ["4层3#会议室"])
        self.assertEqual(result["room_match"]["status"], "unique")
        self.assertEqual(result["room_match"]["strategy"], "numeric_alias")
        self.assertEqual(worker.arguments[0][0]["roomName"], "")

    def test_broad_floor_name_is_not_mistaken_for_room_number(self):
        snapshot = {
            "roomsInfo": [
                {"roomId": "room-3", "roomName": "4层3#会议室"},
                {"roomId": "room-2", "roomName": "4层2#会议室"},
            ]
        }

        with self.assertRaisesRegex(MeetingRoomContractMismatch, "ambiguous"):
            resolve_room("4层", snapshot)

    def test_availability_requires_complete_interval(self):
        with self.assertRaisesRegex(ValueError, "supplied together"):
            list_meeting_room_availability(
                FakeAdapter(),
                FakeMeetingRoomWorker(),
                {"start_time": "2026-07-20 14:00"},
            )
        with self.assertRaisesRegex(ValueError, "only_available"):
            list_meeting_room_availability(
                FakeAdapter(),
                FakeMeetingRoomWorker(),
                {"only_available": True},
            )

    def test_my_applications_pushes_submission_filters_and_matches_room_alias_locally(self):
        worker = FakeMeetingRoomWorker()

        result = list_my_meeting_room_applications(
            FakeAdapter(),
            worker,
            {
                "room_name": "3号",
                "application_start_date": "2026-07-01",
                "application_end_date": "2026-07-31",
                "audit_status": "approved",
                "limit": 50,
            },
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["audit_status"], "approved")
        self.assertEqual(result["items"][0]["audit_status_label"], "审核通过")
        self.assertEqual(result["items"][0]["applied_at"], "2026-07-18 09:30")
        self.assertEqual(result["coverage"]["status"], "complete")
        self.assertEqual(result["coverage"]["local_filters"], ["room_name"])
        self.assertEqual(
            worker.arguments[0][1],
            {
                "beginDate": "2026-07-01 00:00",
                "endDate": "2026-07-31 23:59",
                "status": "1",
            },
        )

    def test_my_applications_filters_by_usage_date_and_formal_room_alias(self):
        worker = FakeMeetingRoomWorker()

        result = list_my_meeting_room_applications(
            FakeAdapter(),
            worker,
            {
                "room_name": "三号会议室",
                "usage_start_date": "2026-07-20",
                "usage_end_date": "2026-07-20",
            },
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["room_name"], "4层3#会议室")
        self.assertEqual(result["items"][0]["start_time"], "2026-07-20 14:00")
        self.assertEqual(
            result["coverage"]["local_filters"],
            ["room_name", "usage_start_date", "usage_end_date"],
        )
        self.assertEqual(worker.arguments[0][1], {})

    def test_my_applications_rejects_invalid_date_range(self):
        with self.assertRaisesRegex(ValueError, "cannot be after"):
            list_my_meeting_room_applications(
                FakeAdapter(),
                FakeMeetingRoomWorker(),
                {
                    "application_start_date": "2026-08-01",
                    "application_end_date": "2026-07-01",
                },
            )
        with self.assertRaisesRegex(ValueError, "usage_start_date"):
            list_my_meeting_room_applications(
                FakeAdapter(),
                FakeMeetingRoomWorker(),
                {
                    "usage_start_date": "2026-08-01",
                    "usage_end_date": "2026-07-01",
                },
            )

    def test_backend_contract_error_is_not_treated_as_empty_data(self):
        worker = FakeMeetingRoomWorker(error={"code": "BAD", "message": "contract changed"})
        with self.assertRaises(MeetingRoomContractMismatch):
            list_meeting_room_availability(FakeAdapter(), worker, {})


class FakeAdapter:
    base_url = "http://oa.example.test/seeyon/"


class FakeMeetingRoomWorker:
    def __init__(self, *, error=None):
        self.methods = []
        self.arguments = []
        self.error = error

    def request(self, method, url, *, headers=None, body=None, timeout_seconds=30):
        del method, headers, timeout_seconds
        fields = parse_qs(body or "")
        manager_method = fields["managerMethod"][0]
        arguments = json.loads(fields["arguments"][0])
        self.methods.append(manager_method)
        self.arguments.append(arguments)
        if self.error is not None:
            payload = self.error
        elif manager_method == "roomListInfo":
            payload = {
                "roomsInfo": [
                    {
                        "roomId": "room-3",
                        "roomName": "4层3#会议室",
                        "roomTypeId": "type-1",
                        "seatCount": 18,
                        "needApp": 2,
                    },
                    {
                        "roomId": "room-2",
                        "roomName": "4层2#会议室",
                        "roomTypeId": "type-1",
                        "seatCount": 30,
                        "needApp": 1,
                    },
                ],
                "roomAppsInfo": [
                    {
                        "roomId": "room-3",
                        "appBeginDate": 1784529000000,
                        "appEndDate": 1784532600000,
                        "status": "1",
                        "statusName": "已审核",
                        "meetingName": "private subject",
                        "perName": "张三",
                        "perDeptName": "研发中心",
                        "perId": "person-secret",
                    }
                ],
            }
        elif manager_method == "getMyApps":
            payload = {
                "page": 1,
                "pages": 1,
                "size": 50,
                "total": 1,
                "data": [
                    {
                        "roomAppId": "app-1",
                        "roomId": "room-3",
                        "roomName": "4层3#会议室",
                        "roomSeatCount": 18,
                        "adminNames": "管理员",
                        "meetingId": "meeting-1",
                        "meetingName": "项目讨论",
                        "appDatetime": "2026-07-18 09:30:00",
                        "startDatetime": "2026-07-20 14:00:00",
                        "endDatetime": "2026-07-20 16:00:00",
                        "appStatus": 1,
                        "appStatusName": "审核通过",
                        "usedStatus": 0,
                        "usedStatusName": "未使用",
                    }
                ],
            }
        else:
            raise AssertionError(f"unexpected manager method: {manager_method}")
        return {
            "status": 200,
            "url": url,
            "json": payload,
            "text": json.dumps(payload, ensure_ascii=False),
        }


if __name__ == "__main__":
    unittest.main()
