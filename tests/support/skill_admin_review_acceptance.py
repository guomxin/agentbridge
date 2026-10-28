"""Isolated Admin review page with frozen manual evidence; never approve it.

Run ``python -m tests.support.skill_admin_review_acceptance --home <empty-dir>``.
The underlying service rejects business capabilities and downstream workers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import tempfile
import threading

from agentbridge.admin.application import AdminControlPlane
from agentbridge.admin.server import create_admin_http_server, validate_admin_server_config
from agentbridge.core.mcp_identities import McpIdentityTokenStore
from tests.support.interaction_acceptance import OWNER, free_port, write_metadata
from tests.support.skill_practical_acceptance import PROPOSAL, SkillPracticalAcceptanceFixture
from tests.test_admin_control_plane import PASSWORD


class SkillAdminReviewAcceptanceFixture(SkillPracticalAcceptanceFixture):
    def __init__(self, home):
        super().__init__(home)
        self.control = AdminControlPlane(service=self.service,
            identity_store=McpIdentityTokenStore(self.service.db_path))
        if not self.meta.get('admin_review_seeded'):
            self.control.accounts.create(username='reviewadmin', password=PASSWORD,
                role='admin', must_change_password=False)
            self._seed_review()
            self.meta.update(admin_review_seeded=True, admin_port=free_port())
            write_metadata(self.meta_path, self.meta)
        self.admin_url = f"http://127.0.0.1:{self.meta['admin_port']}"

    def _seed_review(self):
        a = self.service.skill_authoring
        draft = a.save(OWNER, proposal={**PROPOSAL, 'name': '人工复核审批验收助手'},
            request_key='admin-review-draft')
        job = a.workbench.evaluate(OWNER, draft_id=draft['draft_id'], expected_revision=1,
            cases=[{'prompt': '整理成周报：登录问题还没定位，明天继续查。',
                    'contains': ['明天'], 'excludes': ['已解决']}], request_key='admin-review-evaluation')
        a.workbench.run_once(lambda *_: {'text': '计划：明天继续查，尽快明确原因及解决方案。',
                                       'model': 'synthetic-admin-review'})
        a.workbench.review_report(OWNER, job_id=job['job_id'], row_key='0:0:candidate',
            status='problem', note='原文没有说尽快明确原因及解决方案，不应补写。', expected_version=0)
        request = a.submit(OWNER, draft_id=draft['draft_id'], expected_revision=1,
            request_key='admin-review-submission', reason='隔离审批页验收：自动断言通过，人工指出原文外推断。只查看，不批准。')
        # The submitted evidence must keep the original problem even after a
        # later live review changes. This is synthetic setup, not an approval.
        a.workbench.review_report(OWNER, job_id=job['job_id'], row_key='0:0:candidate',
            status='acceptable', note='提交后修改的实时复核，不得替换已冻结的审批证据。', expected_version=1)
        self.meta['admin_review'] = {'draft_id': draft['draft_id'], 'job_id': job['job_id'],
                                    'request_id': request['request_id']}

    def admin_server(self):
        config = validate_admin_server_config(host='127.0.0.1', port=self.meta['admin_port'],
            public_base_url=self.admin_url, tls_cert=None, tls_key=None)
        return create_admin_http_server(config=config, control_plane=self.control)

    def public_metadata(self):
        return {'fixture': 'synthetic-skill-admin-review-v1', 'admin_url': self.admin_url,
                'username': 'reviewadmin', 'synthetic_password': PASSWORD,
                **self.meta['admin_review'],
                'expected': '提交时人工复核：有问题；原文没有说尽快明确原因及解决方案，不应补写。',
                'evidence_boundary': 'Real Admin UI/stores; synthetic output; no business access; view only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path)
    args = parser.parse_args()
    home = args.home or Path(tempfile.mkdtemp(prefix='agentbridge-skill-admin-review-'))
    fixture = SkillAdminReviewAcceptanceFixture(home)
    server = fixture.admin_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(json.dumps({**fixture.public_metadata(), 'home': str(fixture.home), 'pid': os.getpid()},
                     ensure_ascii=False, indent=2), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: fixture.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: fixture.stop_event.set())
    try:
        fixture.stop_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        fixture.close()


if __name__ == '__main__':
    main()
