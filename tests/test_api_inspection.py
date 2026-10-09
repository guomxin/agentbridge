import unittest

from agentbridge.core.api_discovery import inspect_api_response


class ApiInspectionTests(unittest.TestCase):
    def test_inspect_api_response_summarizes_json_items_shape(self):
        replay = {
            "status": 200,
            "ok": True,
            "contentType": "application/json;charset=UTF-8",
            "url": "http://oa.example.test/ajax.do",
            "json": {
                "Name": "My templates",
                "Data": {
                    "dataNum": 1,
                    "pageNo": 0,
                    "items": [
                        {
                            "title": "Seal request",
                            "link": "/collaboration.do?templateId=tpl-1",
                            "openType": "4",
                        }
                    ],
                },
            },
        }

        result = inspect_api_response(replay)

        self.assertEqual(result["response_type"], "json")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["json_keys"], ["Data", "Name"])
        self.assertEqual(result["data_shape"], "Data.items[]")
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["sample_fields"], ["link", "openType", "title"])

    def test_inspect_api_response_summarizes_json_rows_cells_shape(self):
        replay = {
            "status": 200,
            "ok": True,
            "json": {
                "Data": {
                    "dataCount": 1,
                    "rows": [
                        {
                            "cells": [
                                {"cellContentHTML": "Weekly report", "linkURL": "/summary"},
                                {"cellContentHTML": "Alice"},
                            ]
                        }
                    ],
                },
            },
        }

        result = inspect_api_response(replay)

        self.assertEqual(result["data_shape"], "Data.rows[].cells[]")
        self.assertEqual(result["item_count"], 1)
        self.assertEqual(result["sample_fields"], ["cellContentHTML", "linkURL"])


if __name__ == "__main__":
    unittest.main()
