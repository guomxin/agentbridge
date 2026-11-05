#!/usr/bin/env python3
"""Isolated history/retention QA using real stores and synthetic downstream only."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import signal
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentbridge.core.timeline_attachments import public_attachment, TimelineAttachmentStore
from agentbridge.auth.server import create_auth_http_server, validate_auth_server_config
from agentbridge.auth.card import TrustedAuthApplication
from agentbridge.auth.timeline_attachment import TrustedTimelineAttachmentApplication
from tests.support.interaction_acceptance import SyntheticBroker
from tests.support.latency_acceptance import LatencyAcceptanceFixture, LatencyContract
from tests.support.interaction_acceptance import OWNER, write_metadata
from tests.support.file_retention_acceptance import seed_retained_csv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    if args.home.exists():
        raise SystemExit('Use a fresh isolated home')
    f = LatencyAcceptanceFixture(args.home, seed=False,
        latency=LatencyContract(io_delay_seconds=0, worker_start_seconds=0))
    f.seed_history(messages=160, tasks=35)
    old = f.add_query_case(case_key='expired-read-only', extra_profiles=('missed_punch', 'weekly_report'))
    with f.service.tasks._connect() as db:
        db.execute('UPDATE operations SET finished_at=? WHERE operation_id=?',
            ((datetime.now(timezone.utc)-timedelta(minutes=11)).isoformat(), old['operation_id']))
    # Real report store and permissions; its query bytes are explicitly synthetic.
    csv_row, _ = f.service.tasks.ensure_task(user_subject=OWNER, agent_host='openclaw',
        host_task_key='retained-csv', origin_endpoint_id=f.account['endpoint_id'],
        active_conversation_ref=f.account['openclaw_session_key'], title='合成文件：入口已过期，原报告仍保留')
    csv_task = csv_row['task_id']
    csv = seed_retained_csv(f.service, user_subject=OWNER, task_id=csv_task)
    f.service.tasks.complete_task(task_id=csv_task, user_subject=OWNER, reason='synthetic-retention')
    # A real PNG plus an already expired payload, without changing production TTL.
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
    for label, seconds in [('可用原图', 86400), ('已过期原图', -1), ('即将到期原图', 90)]:
        key = 'history-media:' + label
        media_store = TimelineAttachmentStore(f.service.tasks.db_path,
            clock=lambda seconds=seconds:datetime.now(timezone.utc)-timedelta(days=7)+timedelta(seconds=seconds))
        records = media_store.create_many(user_subject=OWNER, message_key=key,
            attachments=[{'fileName':label+'.png', 'mimeType':'image/png', 'content':base64.b64encode(png).decode()}],
            media_base_url=f.card_url)
        f.service.tasks.append_timeline_message(user_subject=OWNER, source_endpoint_id=f.account['endpoint_id'],
            message_key=key, role='user', text=label+'（合成历史图片）',
            payload={'attachments':[public_attachment(records[0])]})
    long_text = '合成长回答完整性验收。' + ('长文段落，保持原样。\n' * 5200) + '【完整正文结束】'
    f.service.tasks.append_workspace_response(user_subject=OWNER, source_endpoint_id=f.account['endpoint_id'],
        message_key='workspace:assistant:history-long-answer', text=long_text)
    # More than one hundred real isolated interactions, with only the final
    # authorization still live. Never consume an authorization or run a writer.
    large, _ = f.service.tasks.ensure_task(user_subject=OWNER, agent_host='openclaw',
        host_task_key='history-large-batch', origin_endpoint_id=f.account['endpoint_id'],
        active_conversation_ref=f.account['openclaw_session_key'], title='合成大批次：第103条当前入口')
    authorization_clock = f.service.write_authorizations.clock
    for index in range(103):
        # Create earlier records under an isolated clock; keep every immutable
        # authorization and its normal TTL untouched after creation.
        f.service.write_authorizations.clock = (lambda: datetime.now(timezone.utc)-timedelta(hours=1)) if index < 102 else authorization_clock
        operation, _ = f.service.operations.create(user_subject=OWNER, capability_name='oa.efficiency_data.approve',
            capability_version='1', input_summary={'synthetic':True})
        authorization = f.service.write_authorizations.create(user_subject=OWNER, system_id='oa', session_id='synthetic-session',
            capability_name='oa.efficiency_data.approve', capability_version='1', prepare_operation_id=operation['operation_id'],
            plan={'target':operation['operation_id']}, summary={'title':f'合成交互第{index+1}项', 'fields':[]}, card_base_url=f.card_url,
            ttl_seconds=600)
        interaction = f.service._execution_authorization_interaction(authorization)
        f.service.observe_host_task(user_subject=OWNER, task_id=large['task_id'], interaction_ids=[interaction['interactionId']])
    f.service.write_authorizations.clock = authorization_clock
    current_interaction = interaction['interactionId']
    original_cards, workspace = f.servers()
    original_cards.server_close()
    cards = create_auth_http_server(config=validate_auth_server_config(host='127.0.0.1', port=f.meta['card_port'],
        public_base_url=f.card_url, tls_cert=None, tls_key=None),
        application=TrustedAuthApplication(challenge_store=f.service.challenges, broker=SyntheticBroker(f.service.challenges)),
        timeline_attachment_application=TrustedTimelineAttachmentApplication(attachment_store=f.service.timeline_attachments))
    servers = (cards, workspace)
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    metadata = {**f.public_metadata(), 'expired_query_task':old['task_id'], 'csv_task':csv_task,
        'csv_report_id':csv['report_id'], 'long_text_length':len(long_text), 'large_task_id':large['task_id'], 'current_interaction_id':current_interaction}
    write_metadata(f.home/'public.json', metadata)
    print(json.dumps({'workspace_url':f.workspace_url, 'home':str(f.home),
        'expired_query_task':old['task_id'], 'csv_task':csv_task, 'long_text_length':len(long_text)}), flush=True)
    signal.signal(signal.SIGTERM, lambda *_:f.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_:f.stop_event.set())
    try:
        while not f.stop_event.wait(.5):
            write_metadata(f.home/'ledger.json', f.audit())
    finally:
        for server in servers:
            server.shutdown(); server.server_close()
        f.close()


if __name__ == '__main__':
    main()
