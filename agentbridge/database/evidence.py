"""Human-readable, authenticated source links and transport-sized evidence pages."""
import hashlib
import json
from urllib.parse import quote

from agentbridge.database.content import plain_text, CONTENT_MODES, COMMENT_MODES


def response_chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str, indent=2).encode('utf-16-le')) // 2




def text_revision(content):
    return hashlib.sha256(plain_text(content).encode('utf-8')).hexdigest()


def source_reference(source_id, row, base_url=''):
    log_id = row.get('work_log_id', row['id'])
    content = row.get('log_content', row.get('content', ''))
    revision = text_revision(content)
    path = f'/database/logs/{quote(source_id, safe="")}/{log_id}'
    return {'source_url': base_url.rstrip('/') + path + '?revision=' + revision if base_url else None,
            'source_label': f"{row.get('fullname') or row.get('username') or '未署名'} · {row.get('log_date', '')} 日志",
            'source_revision_hash': revision}


def prepare_evidence_page(result, plan, arguments, base_url=''):
    """Trim by serialized character budget; retain complete pagination metadata."""
    rows = result['rows']
    full_scope = result.get('resolved_department_scope')
    scope_fingerprint = hashlib.sha256(json.dumps(full_scope, sort_keys=True).encode()).hexdigest() if full_scope else None
    if not arguments.get('include_diagnostics', False):
        for key in ('executed_sql', 'columns', 'query_scope', 'notes'):
            result.pop(key, None)
        if full_scope and len(full_scope.get('departments', [])) > 20:
            result['resolved_department_scope'] = {k:v for k,v in full_scope.items() if k!='departments'}
            result['resolved_department_scope'].update(department_count=len(full_scope['departments']),
                fingerprint=scope_fingerprint, members_included=False,
                roots=[d for d in full_scope['departments'] if d['id'] in full_scope['root_ids']],
                detail_note='完整成员清单省略；可用目录逐级查看（独立请求非冻结快照），或按需 include_diagnostics=true。')
    result['analysis'] = {'status':'evidence_ready', 'mode':plan.mode or 'read', 'performed_by':'calling_agent',
        'instructions':[(COMMENT_MODES if plan.comments else CONTENT_MODES).get(plan.mode, '返回原始正文证据；需要业务归纳时按当前适用的业务助手执行。'),
                        '记录按 next_cursor 读至 has_more=false；单篇片段按 log_id、text_offset=next_text_offset、expected_revision=source_revision_hash、同一模式续读至 next_text_offset=null，合并后才算全文。']}
    if plan.comments:
        result['analysis']['instructions'].append('评论来源链接只定位关联日志，不冒充评论原文；收到不等于完成。')
    texts = {str(row['id']): plain_text(row.get('content', '')) for row in rows}
    for row in rows:
        row.update(source_reference(result['source_id'], row, base_url))
        if arguments.get('expected_revision') and arguments['expected_revision'] != row['source_revision_hash']:
            from agentbridge.database.independent import DatabaseRejected
            raise DatabaseRejected('DATABASE_CONTENT_CHANGED')
        if plan.evidence_format == 'compact':
            # Paragraphs carry the text once; raw/full text remains in the source viewer.
            if not row.get('passages'):
                row['passages'] = [{'evidence_id':row['evidence_id']+f':paragraph:{i}', 'text':line.strip()}
                                   for i,line in enumerate(texts[str(row['id'])].split('\n'),1) if line.strip()]
            row.pop('content', None)
            row.pop('plain_text', None)
            row.pop('matches', None)
            if plan.comments:
                row.pop('log_content', None)  # log_plain_text remains for comment context.
    def manifest():
        if 'analysis' in result:
            result['analysis']['coverage_manifest'] = [
                {'evidence_id': row['evidence_id'],
                 'paragraph_count': len(row.get('passages', [])),
                 'content_complete': row.get('content_complete', True)} for row in rows]
    def size():
        manifest()
        return response_chars(result) + 64  # Reserve the MCP success envelope.
    def fragment(row, offset, length):
        text = texts[str(row['id'])]
        end = min(len(text), offset + length)
        cursor = 0
        passages = []
        for i, line in enumerate(text.split('\n'), 1):
            lo, hi = max(offset, cursor), min(end, cursor + len(line))
            if hi > lo and line[lo-cursor:hi-cursor].strip():
                passages.append({'evidence_id': row['evidence_id'] + f':paragraph:{i}', 'text': line[lo-cursor:hi-cursor]})
            cursor += len(line) + 1
        row['passages'] = passages
        row.pop('content', None)
        row.pop('plain_text', None)
        row.pop('matches', None)
        row['text_offset'] = offset
        row['next_text_offset'] = end if end < len(text) else None
        row['content_complete'] = offset == 0 and end == len(text)
        row['total_text_chars'] = len(text)
    if 'text_offset' in arguments and rows:
        offset = arguments['text_offset']
        if offset > len(texts[str(rows[0]['id'])]):
            from agentbridge.database.independent import DatabaseRejected
            raise DatabaseRejected('INVALID_DATABASE_ARGUMENTS')
        fragment(rows[0], offset, 2000)
    def metadata():
        result['returned'] = len(rows)
        if rows and result['has_more']:
            last = rows[-1]
            cursor = {'date': str(last[plan.cursor_column]), 'id': str(last['id']), 'scope': plan.scope}
            if plan.include_descendants:
                cursor['department_scope'] = scope_fingerprint
            result['next_cursor'] = cursor
        result['content_complete'] = all(row.get('content_complete', True) for row in rows)
        if arguments.get('include_diagnostics', False):
            result['columns'] = list(rows[0]) if rows else result.get('columns', [])
    metadata()
    while len(rows) > 1 and size() > plan.max_chars:
        rows.pop()
        result['has_more'] = result['truncated'] = True
        metadata()
    if rows and size() > plan.max_chars and not plan.comments:
        length = min(2000, len(texts[str(rows[0]['id'])]))
        while length > 0:
            fragment(rows[0], arguments.get('text_offset', 0), length)
            metadata()
            if size() <= plan.max_chars:
                break
            length //= 2
    if size() > plan.max_chars:
        from agentbridge.database.independent import DatabaseRejected
        needed = size()
        can_retry = needed <= 14000 or arguments.get('include_diagnostics', False)
        raise DatabaseRejected('DATABASE_EVIDENCE_TOO_LARGE', recovery={
            'action':'adjust_transport' if can_retry else 'capacity_limit', 'retry_same_request':False,
            'requested_max_chars':plan.max_chars, 'minimum_current_response_chars':needed,
            'arguments_patch':{'max_chars':min(14000,max(12000,needed)), 'include_diagnostics':False} if can_retry else {},
            'preserve_filters':True,
            'instruction':'保留来源、能力、筛选与游标，合并 arguments_patch 后重试；不反复缩小 page_size，不改业务范围，不切自由 SQL。' if can_retry else
                          '最小响应仍超上限，本次没有完成读取；需改进传输或由用户明确调整需求，不自动缩小范围。'})
    return result
