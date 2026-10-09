import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agentbridge.core.discovered import DiscoveredApiStore


class DiscoveredApiStoreTests(unittest.TestCase):
    def test_list_and_load_saved_api_metadata(self):
        with TemporaryDirectory() as tmp:
            api_dir = Path(tmp) / "discovered" / "oa" / "apis"
            api_dir.mkdir(parents=True)
            (api_dir / "template-section.json").write_text(
                json.dumps(
                    {
                        "schema_version": "agentbridge.discovered_api.v1",
                        "name": "template-section",
                        "system": "oa",
                        "description": "Template section projection",
                        "request": {"method": "GET", "url": "http://oa.example.test/ajax.do"},
                        "inspection": {
                            "data_shape": "Data.items[]",
                            "item_count": 36,
                            "sample_fields": ["title", "link"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            store = DiscoveredApiStore(Path(tmp))
            apis = store.list_apis("oa")
            loaded = store.load_api("oa", "template-section")

            self.assertEqual([api.name for api in apis], ["template-section"])
            self.assertEqual(loaded.description, "Template section projection")
            self.assertEqual(loaded.access, "read")
            self.assertEqual(loaded.risk, "low")
            self.assertEqual(loaded.request["method"], "GET")
            self.assertEqual(loaded.tool_name, "oa__discovered__template_section")
            self.assertEqual(loaded.command_name, "discovered:template-section")

    def test_load_api_exposes_parameter_schema(self):
        with TemporaryDirectory() as tmp:
            api_dir = Path(tmp) / "discovered" / "oa" / "apis"
            api_dir.mkdir(parents=True)
            (api_dir / "search.json").write_text(
                json.dumps(
                    {
                        "schema_version": "agentbridge.discovered_api.v1",
                        "name": "search",
                        "system": "oa",
                        "request": {
                            "method": "GET",
                            "url": "http://oa.example.test/ajax.do?q={{keyword}}&page={{page}}",
                        },
                        "parameters": {
                            "keyword": {
                                "type": "string",
                                "required": True,
                                "description": "Search keyword",
                            },
                            "page": {"type": "integer"},
                        },
                        "inspection": {"data_shape": "Data.items[]"},
                    }
                ),
                encoding="utf-8",
            )

            loaded = DiscoveredApiStore(Path(tmp)).load_api("oa", "search")

            self.assertEqual(
                loaded.parameters,
                {
                    "keyword": {
                        "type": "string",
                        "required": True,
                        "description": "Search keyword",
                    },
                    "page": {"type": "integer"},
                },
            )

    def test_load_api_rejects_path_traversal_names(self):
        with TemporaryDirectory() as tmp:
            store = DiscoveredApiStore(Path(tmp))

            with self.assertRaises(ValueError):
                store.load_api("oa", "../secret")


if __name__ == "__main__":
    unittest.main()
