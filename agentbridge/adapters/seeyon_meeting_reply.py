"""Current meeting-invitation replies, independent of collaboration approval."""
from copy import deepcopy
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qs, urlparse

from agentbridge.adapters.seeyon_meeting import _ajax, MeetingContractMismatch
from agentbridge.core.capability import CapabilitySpec
from agentbridge.core.write_workflow import WriteWorkflowDefinition

PREPARE = 'oa.meeting.reply.prepare'
COMMIT = 'oa.meeting.reply'
VERSION = 'seeyon-meeting-reply-v1'
ATTITUDES = {'join': (1, '参加'), 'not_join': (0, '不参加'), 'pending': (-1, '待定')}
SCHEMA = {
    'schema_version': 'agentbridge.oa_meeting_reply_fields.v1', 'title': '核对会议并回复',
    'system': '致远 OA', 'effect': '向会议发起人提交本人的参会回执',
    'submit_label': '提交字段', 'notice': '请选择本人的参会意向，核对后再确认回复。',
    'fields': [
        {'name': 'attitude', 'label': '参会意向', 'control': 'select', 'required': True,
         'options': [{'value': key, 'label': value[1]} for key, value in ATTITUDES.items()]},
        {'name': 'feedback', 'label': '回复说明', 'control': 'textarea', 'required': False,
         'max_length': 1000, 'rows': 3},
    ],
}
INPUT = {'type': 'object', 'properties': {
    'affair_id': {'type': 'string', 'minLength': 1, 'maxLength': 256},
    'input_submission_id': {'type': 'string'},
}, 'required': ['affair_id'], 'additionalProperties': False}


class MeetingReplyContractMismatch(RuntimeError):
    pass


class MeetingReplyOutcomeUnknown(RuntimeError):
    pass


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'-?[0-9]{1,20}', value):
        raise MeetingReplyContractMismatch('会议或回执缺少有效标识。')
    return value


def meeting_target(adapter, source):
    url = urlparse(str(source.get('href') or ''))
    query = parse_qs(url.query, keep_blank_values=True)
    if (source.get('category') != '会议' or f'{url.scheme}://{url.netloc}' != adapter.origin
            or url.path != '/seeyon/meeting.do' or query.get('method') != ['view']
            or len(query.get('meetingId', [])) != 1
            or query.get('proxyId', ['']) != [''] or url.username or url.password):
        raise MeetingReplyContractMismatch('仅支持当前本人待办中的会议邀请，不支持代理回复。')
    return {'affair_id': _id(source.get('affair_id')), 'meeting_id': _id(query['meetingId'][0])}


def _resolve(adapter, worker, affair_id):
    _id(affair_id)
    pending = adapter._fetch_workflow_collection(worker, 'pending')
    rows = pending.get('items')
    if (not isinstance(rows, list) or type(pending.get('total')) is not int
            or pending['total'] != len(rows) or pending.get('count') != len(rows)
            or len({str(r.get('affair_id')) for r in rows}) != len(rows)):
        raise MeetingReplyContractMismatch('会议待办来源不完整，请重新查询。')
    matches = [row for row in rows if row.get('affair_id') == affair_id]
    if len(matches) != 1:
        raise MeetingReplyContractMismatch('该会议已不在本人的当前待办中。')
    source = matches[0]
    target = meeting_target(adapter, source)
    view = _view(adapter, worker, target['meeting_id'])
    frozen = _snapshot(view, target['meeting_id'])
    if frozen['meeting']['title'] != source.get('title'):
        raise MeetingReplyContractMismatch('会议标题与待办来源不一致。')
    return {**target, **frozen}


def _view(adapter, worker, meeting_id):
    try:
        return _ajax(worker, adapter, 'meetingView', [{'meetingId': meeting_id, 'proxyId': ''}])
    except MeetingContractMismatch as exc:
        raise MeetingReplyContractMismatch('会议详情接口契约无法核验。') from exc


_MEETING_FIELDS = ('id', 'title', 'createUser', 'createUserName', 'emceeName', 'recorderName',
                   'beginDate', 'endDate', 'meetPlace', 'state', 'roomState', 'content',
                   'conferees', 'impart', 'leader', 'bodyType', 'updateDateStr', 'attachments')
_REPLY_FIELDS = ('id', 'userId', 'userName', 'meetingId', 'feedbackFlag', 'feedback', 'attachmentJSON')


def _snapshot(view, meeting_id, *, require_reply=True):
    if not isinstance(view, dict):
        raise MeetingReplyContractMismatch('会议详情响应无效。')
    meeting, reply, auth = view.get('meetingVo'), view.get('myReply'), view.get('meetingAuth')
    if not all(isinstance(x, dict) for x in (meeting, reply, auth)):
        raise MeetingReplyContractMismatch('会议详情缺少本人回执或权限信息。')
    if _id(meeting.get('id')) != meeting_id or _id(reply.get('meetingId')) != meeting_id:
        raise MeetingReplyContractMismatch('会议与本人回执标识不一致。')
    _id(reply.get('id')); _id(reply.get('userId'))
    if not all(isinstance(meeting.get(k), str) and meeting[k] for k in ('title', 'beginDate', 'endDate', 'bodyType')):
        raise MeetingReplyContractMismatch('会议标题、时间或正文类型不完整。')
    if not isinstance(meeting.get('content'), str) or len(meeting['content']) > 262144:
        raise MeetingReplyContractMismatch('会议正文缺失或超过完整展示上限。')
    attachments = meeting.get('attachments')
    if not isinstance(attachments, list) or len(attachments) > 100 or any(
            not isinstance(a, dict) or not (a.get('filename') or a.get('name')) for a in attachments):
        raise MeetingReplyContractMismatch('会议附件目录不完整。')
    if require_reply:
        if (auth.get('showReply') is not True or auth.get('showReplyAttitude') is not True
                or type(meeting.get('state')) is not int or meeting['state'] not in (10, 20)
                or type(meeting.get('roomState')) is not int or meeting['roomState'] != 1
                or type(reply.get('feedbackFlag')) is not int or reply['feedbackFlag'] != -100):
            raise MeetingReplyContractMismatch('会议当前不允许首次参会回复，或本人已经回复。')
        if reply.get('attachmentJSON') not in ('[]', None, ''):
            raise MeetingReplyContractMismatch('本人原回执包含附件，当前不支持修改该回执。')
        if meeting['bodyType'] != '10':
            raise MeetingReplyContractMismatch('会议正文类型尚未支持，请到 OA 核对。')
    return {'meeting': {k: deepcopy(meeting.get(k)) for k in _MEETING_FIELDS},
            'reply': {k: deepcopy(reply.get(k)) for k in _REPLY_FIELDS}}


class _BodyText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.images = 0; self.hidden = 0
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'): self.hidden += 1
        if tag == 'img': self.images += 1
        if tag in ('p', 'br', 'div', 'li'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script', 'style'): self.hidden = max(0, self.hidden - 1)
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)


def meeting_review(target):
    meeting, reply = target['meeting'], target['reply']
    body = _BodyText(); body.feed(meeting.get('content') or '')
    text = '\n'.join(line.strip() for line in ''.join(body.parts).splitlines() if line.strip())
    fields = [{'label': label, 'value': str(meeting.get(key) or '未填写')} for key, label in (
        ('title', '会议主题'), ('beginDate', '开始时间'), ('endDate', '结束时间'),
        ('meetPlace', '地点'), ('createUserName', '发起人'), ('emceeName', '主持人'), ('recorderName', '记录人'))]
    fields += [{'label': '回复人', 'value': reply['userName']},
               {'label': '当前回执', 'value': {-100:'未回执', 1:'参加', 0:'不参加', -1:'待定'}.get(reply['feedbackFlag'], '未知')}]
    if body.images:
        text += f'\n正文另含 {body.images} 张图片；图片尚未读取，可在下方逐项查看预览状态。'
    from agentbridge.adapters.seeyon_material_preview import material_references, public_manifest
    previews = public_manifest(material_references(text=meeting.get('content'), attachments=meeting.get('attachments') or []))
    return {'title': '会议邀请', 'kind': 'meeting_reply', 'materialsVersion': VERSION, 'fields': fields,
            'materialPreviews': previews,
            'sections': [{'id':'meeting', 'title':'会议详情', 'kind':'facts', 'fields':fields},
                         {'id':'body', 'title':'会议说明', 'kind':'text', 'text':text}],
            'attachments': [{'name': str(a.get('filename') or a.get('name'))} for a in meeting.get('attachments') or []],
            'completeness': {'status': 'complete', 'scope': 'meeting_metadata_body_text_attachment_names',
                             'bodyImageCount': body.images, 'imageContentsLoaded': False, 'attachmentContentsLoaded': False},
            'truncated': False}


def preflight_meeting_reply(adapter, worker, arguments, profile='meeting_reply'):
    target = _resolve(adapter, worker, arguments.get('affair_id'))
    return {'matched': True, 'profile': profile, 'review': meeting_review(target), '_content_target': target,
            'review_fingerprint': _hash(target)}


def prepare_meeting_reply(adapter, worker, arguments):
    attitude, feedback = arguments.get('attitude'), arguments.get('feedback', '')
    if not isinstance(attitude, str) or attitude not in ATTITUDES or not isinstance(feedback, str) or len(feedback) > 1000:
        raise ValueError('请选择参加、不参加或待定，并填写不超过 1000 字的回复说明。')
    target = _resolve(adapter, worker, arguments.get('affair_id'))
    target['review_fingerprint'] = _hash(target)
    exact = {'attitude': attitude, 'feedback': feedback}
    payload = {'meetingId': target['meeting_id'], 'proxyId': '',
               'feedbackFlag': str(ATTITUDES[attitude][0]), 'feedback': feedback, 'atMembers': []}
    review = meeting_review(target)
    return {'plan': {'schema_version': 'agentbridge.oa_meeting_reply_plan.v1',
        'business_intent':'reply_meeting', 'target':target, 'exact_input':exact, 'exact_payload':payload,
        'action_contract': {'version':VERSION, 'verification':'exact_meeting_and_my_reply'},
        'expected_effect': {'meeting_reply': attitude, 'submitted_count':1}},
        'summary': {**review, 'title':'核对会议并回复', 'effect':f'向会议发起人回复：{ATTITUDES[attitude][1]}',
                    'fields':review['fields']+[{'label':'参会意向','value':ATTITUDES[attitude][1]},
                                             {'label':'回复说明','value':feedback or '无'}]}}


def commit_meeting_reply(adapter, worker, plan, *, enter_commit_boundary):
    target, exact = plan.get('target') or {}, plan.get('exact_input') or {}
    if (plan.get('schema_version') != 'agentbridge.oa_meeting_reply_plan.v1'
            or plan.get('action_contract') != {'version':VERSION,'verification':'exact_meeting_and_my_reply'}):
        raise MeetingReplyContractMismatch('会议回复计划版本无效。')
    fresh = prepare_meeting_reply(adapter, worker, {'affair_id':target.get('affair_id'), **exact})['plan']
    if any(fresh[key] != plan.get(key) for key in ('target','exact_input','exact_payload','expected_effect')):
        raise MeetingReplyContractMismatch('会议内容或本人回执已变化，请重新核对后确认。')
    enter_commit_boundary()
    try:
        _ajax(worker, adapter, 'reply', [fresh['exact_payload']])
        after = _snapshot(_view(adapter, worker, target['meeting_id']), target['meeting_id'], require_reply=False)
        reply = after['reply']
        if (any(reply[key] != target['reply'][key] for key in ('id','userId','meetingId'))
                or type(reply['feedbackFlag']) is not int or reply['feedbackFlag'] != ATTITUDES[exact['attitude']][0]
                or (reply['feedback'] or '') != exact['feedback']):
            raise MeetingReplyOutcomeUnknown('会议回复结果未能与授权选择及本人身份匹配，请核对原任务，勿重复提交。')
        return {'submitted':True, 'verified':True, 'verification':{'confirmed':True,
                'method':'exact_meeting_and_my_reply', 'meeting_id':target['meeting_id'],
                'reply_id':reply['id'], 'attitude':exact['attitude']},
                'summary':fresh['expected_effect']}
    except MeetingReplyOutcomeUnknown:
        raise
    except Exception as exc:
        raise MeetingReplyOutcomeUnknown('会议回复已进入提交边界，结果暂未确认；请核对，勿重复提交。') from exc


MEETING_REPLY_WORKFLOW = WriteWorkflowDefinition(
    prepare_spec=CapabilitySpec(PREPARE,'0.1.0','Review one current meeting invitation and collect an explicit attendance response in a trusted card.',INPUT,{'type':'object'},'controlled_write','seeyon-central','meeting-reply-prepare-v1'),
    commit_spec=CapabilitySpec(COMMIT,'0.1.0','Consume one authorization, recheck the exact meeting and reply once, then verify the current user receipt.',{'type':'object','properties':{'authorization_id':{'type':'string'}},'required':['authorization_id'],'additionalProperties':False},{'type':'object'},'controlled_write','seeyon-central','meeting-reply-commit-v1'),
    required_scopes=frozenset({'oa:write:meeting'}), field_schema=SCHEMA, context_fields=('affair_id',),
    prepare_function=prepare_meeting_reply, commit_function=commit_meeting_reply,
    contract_error=MeetingReplyContractMismatch, outcome_error=MeetingReplyOutcomeUnknown,
    field_message='请在可信办理卡选择本人的参会意向。', authorization_message='请核对会议和参会意向后确认回复。',
    preflight_function=preflight_meeting_reply, preflight_profile='meeting_reply',
)
