import unittest

from agentbridge.core.api_discovery import extract_api_candidates


class ApiCandidateTests(unittest.TestCase):
    def test_extract_api_candidates_groups_network_records(self):
        snapshot = {
            "records": [
                {
                    "kind": "fetch",
                    "method": "POST",
                    "url": "http://10.10.50.110/seeyon/rest/pending/list",
                    "status": 200,
                    "requestBody": '{"page":1}',
                },
                {
                    "kind": "fetch",
                    "method": "POST",
                    "url": "http://10.10.50.110/seeyon/rest/pending/list",
                    "status": 200,
                    "requestBody": '{"page":2}',
                },
                {
                    "kind": "xmlhttprequest",
                    "method": "GET",
                    "url": "http://10.10.50.110/seeyon/rest/message/count",
                    "status": 200,
                },
            ],
            "resources": [
                {
                    "name": "http://10.10.50.110/seeyon/common/app.js",
                    "initiatorType": "script",
                },
                {
                    "name": "http://10.10.50.110/seeyon/ajax.do?method=search",
                    "initiatorType": "xmlhttprequest",
                },
            ],
        }

        candidates = extract_api_candidates(snapshot)

        self.assertEqual(
            [(item["method"], item["path"], item["count"]) for item in candidates],
            [
                ("POST", "/seeyon/rest/pending/list", 2),
                ("GET", "/seeyon/rest/message/count", 1),
                ("GET", "/seeyon/ajax.do?method=search", 1),
            ],
        )
        self.assertEqual(candidates[0]["sample_request_body"], '{"page":1}')
        self.assertEqual(candidates[0]["statuses"], [200])
        self.assertNotIn("app.js", [item["path"] for item in candidates])


if __name__ == "__main__":
    unittest.main()
