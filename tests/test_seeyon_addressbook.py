import unittest
from unittest.mock import patch

from agentbridge.adapters.seeyon_addressbook import (
    ADDRESSBOOK_DEPARTMENT_MEMBERS_CAPABILITY,
    ADDRESSBOOK_EXPORT_CAPABILITY,
    ADDRESSBOOK_GROUP_LIST_CAPABILITY,
    ADDRESSBOOK_GROUP_MEMBERS_CAPABILITY,
    ADDRESSBOOK_INPUT_SCHEMAS,
    ADDRESSBOOK_ORGANIZATION_TREE_CAPABILITY,
    ADDRESSBOOK_PERSON_SEARCH_CAPABILITY,
    ADDRESSBOOK_PRIVATE_CONTACT_SEARCH_CAPABILITY,
    SeeyonAddressbookContractMismatch,
    _decode_reference,
    _member_row,
    department_members,
    export_addressbook,
    group_list,
    group_members,
    organization_tree,
    person_get,
    person_search,
    private_contact_search,
)
from agentbridge.adapters.seeyon_central import build_central_capability_registry
from agentbridge.adapters.base import AdapterLoginRequired
from agentbridge.adapters.seeyon_addressbook import _organization_nodes
import json
from unittest.mock import Mock


BASE_URL = "http://oa.example.test/seeyon/main.do?method=main"


class SeeyonAddressbookTests(unittest.TestCase):
    def test_tree_readiness_wait_keeps_initial_observation_without_reloading(self):
        frame=Mock(url='https://oa.invalid/tree')
        page=Mock(url='https://oa.invalid/home')
        frame.evaluate.side_effect=[
            {'documentState':'loading'},
            {'documentState':'complete','jqueryPresent':True,'pluginPresent':True,'treePresent':True},
            [_node('1','','fixture',[],True)],
        ]
        nodes, observations=_organization_nodes(page,frame)
        self.assertEqual(len(nodes),1)
        self.assertEqual([o['treePresent'] for o in observations],[False,True])
        page.wait_for_timeout.assert_called_once_with(250)
        page.goto.assert_not_called()

    def test_tree_missing_object_is_bounded_and_diagnostics_are_sanitized(self):
        frame=Mock(url='https://oa.invalid/tree?secret=never-log')
        page=Mock(url='https://oa.invalid/home')
        frame.evaluate.return_value={'documentState':'complete','secret':'never-log','jqueryPresent':True}
        with self.assertRaises(SeeyonAddressbookContractMismatch) as caught:
            _organization_nodes(page,frame)
        diagnostic=json.loads(str(caught.exception))
        self.assertEqual(diagnostic['code'],'OA_ORG_TREE_OBJECT_MISSING')
        self.assertEqual(len(diagnostic['observations']),4)
        self.assertEqual(page.wait_for_timeout.call_count,3)
        self.assertNotIn('never-log',str(caught.exception))

    def test_tree_login_fallback_is_not_retried_as_missing_object(self):
        page=Mock(url='https://oa.invalid/seeyon/login/index.jsp')
        frame=Mock(url='https://oa.invalid/tree')
        with self.assertRaises(AdapterLoginRequired):
            _organization_nodes(page,frame)
        frame.evaluate.assert_not_called()
        page.wait_for_timeout.assert_not_called()

    def test_tree_parse_failure_and_empty_visibility_are_not_guessed_as_permissions(self):
        for nodes,code in [([None],'OA_ORG_TREE_PARSE_FAILED'),([{}],'OA_ORG_TREE_PARSE_FAILED'),([], 'OA_ORG_TREE_EMPTY_VISIBILITY_UNDETERMINED')]:
            with self.subTest(code=code):
                page=Mock(url='https://oa.invalid/home'); frame=Mock(url='https://oa.invalid/tree')
                frame.evaluate.side_effect=[{'documentState':'complete','treePresent':True},nodes]
                with self.assertRaises(SeeyonAddressbookContractMismatch) as caught:
                    _organization_nodes(page,frame)
                self.assertEqual(json.loads(str(caught.exception))['code'],code)
                page.wait_for_timeout.assert_not_called()

    def test_home_and_frame_failures_preserve_stage_without_browser_text(self):
        module='agentbridge.adapters.seeyon_addressbook.'
        with patch(module+'_open_home',side_effect=RuntimeError('secret-browser-url')):
            with self.assertRaises(SeeyonAddressbookContractMismatch) as caught:
                organization_tree(object(),base_url=BASE_URL,arguments={})
        self.assertEqual(json.loads(str(caught.exception))['code'],'OA_ORG_HOME_UNAVAILABLE')
        self.assertNotIn('secret',str(caught.exception))
        with patch(module+'_open_home',return_value=(Mock(url='https://oa.invalid/home'),'1')), \
                patch(module+'_wait_for_frame',side_effect=RuntimeError('secret-frame-url')):
            with self.assertRaises(SeeyonAddressbookContractMismatch) as caught:
                organization_tree(object(),base_url=BASE_URL,arguments={})
        self.assertEqual(json.loads(str(caught.exception))['code'],'OA_ORG_FRAME_NOT_READY')
        self.assertNotIn('secret',str(caught.exception))

    def test_registry_exposes_the_complete_read_only_package(self):
        registry = build_central_capability_registry()

        for name in ADDRESSBOOK_INPUT_SCHEMAS:
            spec = registry.get(name)
            self.assertEqual(spec.effect, "read")
            self.assertEqual(spec.adapter, "seeyon-central")
        self.assertIn(
            "source", ADDRESSBOOK_INPUT_SCHEMAS[ADDRESSBOOK_EXPORT_CAPABILITY]["required"]
        )

    def test_organization_tree_preserves_hierarchy_and_filters_by_path(self):
        page = FakePage(
            FakeFrame(
                [
                    _node("100", "", "集团", ["集团"], True),
                    _node("200", "100", "研发中心", ["集团", "研发中心"], True),
                    _node("201", "200", "人工智能组", ["集团", "研发中心", "人工智能组"], False),
                ]
            )
        )
        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(page, "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._wait_for_frame",
            return_value=page.frames[0],
        ):
            result = organization_tree(
                object(),
                base_url=BASE_URL,
                arguments={"keyword": "人工智能", "limit": 20},
            )

        self.assertEqual(result["organization"], {"account_id": "100", "name": "集团"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["department_id"], "201")
        self.assertEqual(result["items"][0]["parent_department_id"], "200")

    def test_organization_tree_matches_names_when_paths_are_empty(self):
        page = FakePage(FakeFrame([
            _node("100", "", "集团", [], True),
            _node("200", "100", "照明市场推广中心", [], False),
            _node("201", "100", "智能照明运维中心", [], False),
            _node("202", "100", "研发中心", [], False),
        ]))
        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(page, "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._wait_for_frame",
            return_value=page.frames[0],
        ):
            for keyword, expected in [
                ("照明", ["200", "201"]),
                ("照明市场推广中心", ["200"]),
                ("不存在", []),
                ("", ["200", "201", "202"]),
            ]:
                with self.subTest(keyword=keyword):
                    result = organization_tree(
                        object(), base_url=BASE_URL,
                        arguments={"keyword": keyword, "limit": 500},
                    )
                    self.assertEqual(
                        [item["department_id"] for item in result["items"]], expected
                    )
                    self.assertEqual(result["matched_count"], len(expected))

    def test_person_search_uses_server_filter_and_preserves_masked_values(self):
        raw = {
            "rows": [
                {
                    "person_id": "42",
                    "values": {
                        "": "",
                        "姓名": "张三",
                        "人员编号": "E42",
                        "部门": "研发中心",
                        "岗位": "工程师",
                        "办公电话": "******",
                        "手机号码": "******",
                    },
                }
            ],
            "total": 1,
            "total_pages": 1,
        }
        captured = {}

        def read_list(_worker, *, base_url, query):
            captured.update({"base_url": base_url, "query": query})
            return raw

        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(object(), "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._read_list",
            side_effect=read_list,
        ):
            result = person_search(
                object(),
                base_url=BASE_URL,
                arguments={"query": "张三", "search_type": "all", "limit": 20},
            )

        self.assertEqual(captured["query"]["searchContent"], "张三")
        self.assertEqual(captured["query"]["searchType"], "all")
        self.assertEqual(captured["query"]["pageSize"], 20)
        self.assertEqual(result["items"][0]["mobile_phone"], "******")
        self.assertEqual(
            _decode_reference(result["items"][0]["person_ref"], expected_kind="person"),
            {"id": "42", "name": "张三"},
        )

    def test_person_get_re_resolves_and_rejects_stale_reference(self):
        item = _member_row(
            {
                "person_id": "42",
                "values": {"姓名": "张三", "部门": "研发中心"},
            },
            kind="organization",
        )
        with patch(
            "agentbridge.adapters.seeyon_addressbook.person_search",
            return_value={"items": [item]},
        ):
            result = person_get(
                object(),
                base_url=BASE_URL,
                arguments={"person_ref": item["person_ref"]},
            )
        self.assertEqual(result["item"]["person_id"], "42")
        self.assertEqual(result["detail_visibility"], "directory_row")

        with patch(
            "agentbridge.adapters.seeyon_addressbook.person_search",
            return_value={"items": []},
        ):
            with self.assertRaises(SeeyonAddressbookContractMismatch):
                person_get(
                    object(),
                    base_url=BASE_URL,
                    arguments={"person_ref": item["person_ref"]},
                )

    def test_department_members_controls_descendant_scope(self):
        captured = {}

        def read_list(_worker, *, base_url, query):
            captured.update({"base_url": base_url, "query": query})
            return {"rows": [], "total": 0, "total_pages": 0}

        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(object(), "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._read_list",
            side_effect=read_list,
        ):
            result = department_members(
                object(),
                base_url=BASE_URL,
                arguments={
                    "department_id": "200",
                    "include_descendants": True,
                    "limit": 50,
                },
            )

        self.assertEqual(captured["query"]["deptId"], "200")
        self.assertEqual(captured["query"]["sonDepartmentMembers"], "true")
        self.assertEqual(result["count"], 0)

    def test_group_list_combines_types_and_excludes_tree_roots(self):
        pages = {
            2: FakePage(FakeFrame([_node("-2", "", "联系组", ["联系组"], True), _node("-1", "-2", "未分类联系人", ["联系组", "未分类联系人"], False)])),
            4: FakePage(FakeFrame([_node("-2", "", "个人组", ["个人组"], False)])),
            3: FakePage(FakeFrame([_node("-1", "", "公司", ["公司"], True), _node("300", "-1", "信息化组", ["公司", "信息化组"], False)])),
            6: FakePage(FakeFrame([_node("-1", "", "公司", ["公司"], True), _node("400", "-1", "项目组A", ["公司", "项目组A"], False)])),
        }

        def open_home(_worker, *, base_url, addressbook_type):
            self.assertEqual(base_url, BASE_URL)
            return pages[addressbook_type], "100"

        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home", side_effect=open_home
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._wait_for_frame",
            side_effect=lambda page, _fragment: page.frames[0],
        ):
            result = group_list(object(), base_url=BASE_URL, arguments={})

        self.assertEqual(
            [(item["group_type"], item["name"]) for item in result["items"]],
            [
                ("private", "未分类联系人"),
                ("system", "信息化组"),
                ("project", "项目组A"),
            ],
        )

    def test_group_members_uses_group_type_contract(self):
        captured = {}

        def read_list(_worker, *, base_url, query):
            captured.update({"base_url": base_url, "query": query})
            return {
                "rows": [
                    {
                        "person_id": "42",
                        "values": {
                            "姓名": "张三",
                            "部门": "研发中心",
                            "岗位": "工程师",
                        },
                    }
                ],
                "total": 1,
                "total_pages": 1,
            }

        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(object(), "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._read_list",
            side_effect=read_list,
        ):
            result = group_members(
                object(),
                base_url=BASE_URL,
                arguments={"group_type": "system", "group_id": "300"},
            )

        self.assertEqual(captured["query"]["method"], "listSysTeamMembers")
        self.assertEqual(captured["query"]["tId"], "300")
        self.assertEqual(result["items"][0]["person_id"], "42")

    def test_private_contact_search_uses_private_shape(self):
        with patch(
            "agentbridge.adapters.seeyon_addressbook._open_home",
            return_value=(object(), "100"),
        ), patch(
            "agentbridge.adapters.seeyon_addressbook._read_list",
            return_value={
                "rows": [
                    {
                        "person_id": "7",
                        "values": {
                            "姓名": "联系人甲",
                            "单位名称": "外部单位",
                            "职务级别": "经理",
                            "办公电话": "******",
                            "手机号码": "******",
                        },
                    }
                ],
                "total": 1,
                "total_pages": 1,
            },
        ):
            result = private_contact_search(
                object(),
                base_url=BASE_URL,
                arguments={"query": "联系人甲", "limit": 20},
            )

        item = result["items"][0]
        self.assertEqual(item["company"], "外部单位")
        self.assertEqual(item["mobile_phone"], "******")
        self.assertEqual(
            _decode_reference(item["contact_ref"], expected_kind="contact"),
            {"id": "7", "name": "联系人甲"},
        )

    def test_export_is_bounded_and_omits_internal_references(self):
        result = {
            "source_total": 1,
            "truncated": False,
            "items": [
                {
                    "person_id": "42",
                    "person_ref": "opaque",
                    "name": "张三",
                    "person_code": "E42",
                    "department": "研发中心",
                    "position": "工程师",
                    "office_phone": "******",
                    "mobile_phone": "******",
                }
            ],
        }
        with patch(
            "agentbridge.adapters.seeyon_addressbook.person_search", return_value=result
        ):
            report = export_addressbook(
                object(),
                base_url=BASE_URL,
                arguments={"source": "person_search", "query": "张三", "limit": 500},
            )

        self.assertEqual(report["reportType"], "person_search")
        self.assertEqual(report["metadata"]["maskedValuesPreserved"], True)
        self.assertNotIn("person_ref", report["rows"][0])
        self.assertNotIn("person_id", report["rows"][0])


class FakePage:
    def __init__(self, frame):
        self.frames = [frame]


class FakeFrame:
    def __init__(self, nodes):
        self.nodes = nodes

    def evaluate(self, _script, _arguments):
        if 'documentState:' in _script:
            return {'documentState':'complete','jqueryPresent':True,'pluginPresent':True,'treePresent':True}
        return self.nodes


def _node(identifier, parent_id, name, path, has_children):
    return {
        "id": identifier,
        "parent_id": parent_id,
        "name": name,
        "path": path,
        "has_children": has_children,
    }


if __name__ == "__main__":
    unittest.main()
