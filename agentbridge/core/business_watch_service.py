"""Owned business watches. Background observations never extend user activity."""
from __future__ import annotations

import base64
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import time

from agentbridge.core.business_content_identity import principal_binding
from agentbridge.core.capability import CapabilitySpec
from agentbridge.core.business_watches import BusinessWatchError, BusinessWatchStore

STATE_CAPABILITY = 'oa.workflow.state.get'
WATCH_PERMISSION = 'oa.workflow.watch'


def watch_state_capability():
    return CapabilitySpec(name=STATE_CAPABILITY, version='0.1.0',
        description='Read the exact owned OA workflow state from a short-lived source reference.',
        input_schema={'type':'object','additionalProperties':False,
            'properties':{'target_ref':{'type':'string','minLength':1,'maxLength':8192}},
            'required':['target_ref']}, output_schema={'type':'object'}, effect='read',
        adapter='seeyon-central', workflow='workflow-state-v1')


def _now():
    return datetime.now(timezone.utc)


def public_watch(value):
    fields = {'watch_id','title','state','version','created_at','updated_at','expires_at',
        'interval_seconds','allow_mark_read','last_checked_at','next_check_at','last_error_code',
        'pause_reason','last_verified_at','last_source_observed_at','last_check_status',
        'observation_seq','change_seq','last_success_at','change_sequence','consecutive_failures'}
    result = {k:deepcopy(v) for k,v in value.items() if k in fields}
    result['snapshot'] = deepcopy(value.get('last_snapshot'))
    result['event_types'] = list(value.get('rules') or [])
    result['endpoint_ids'] = list(value.get('endpoints') or [])
    return result


class BusinessWatchService:
    def __init__(self, service):
        self.service = service
        self.store = BusinessWatchStore(service.db_path)

    @property
    def system_instance(self):
        return 'sha256:' + hashlib.sha256(self.service.adapter.base_url.encode()).hexdigest()

    def require_permission(self, subject):
        self.service.user_grants.require(subject, 'oa.workflow.read')
        self.service.user_grants.require(subject, WATCH_PERMISSION)

    def _session(self, subject, *, principal=None):
        session = self.service.sessions.find(user_subject=subject, system_id='oa')
        binding = principal_binding(session) if session else None
        if not session or session['state'] != 'active' or not binding:
            raise BusinessWatchError('WATCH_SESSION_UNAVAILABLE','OA登录已失效，请重新登录后恢复关注。',status=401)
        if principal is not None and binding != principal:
            raise BusinessWatchError('WATCH_PRINCIPAL_CHANGED','OA账号已变化，原关注已暂停。',status=403)
        return session, binding

    def _pack(self, purpose, subject, value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()
        encrypted = self.service.session_states.protector.protect(raw,
            context=f'business-watch:{purpose}:v1:{subject}'.encode())
        return base64.urlsafe_b64encode(encrypted).decode()

    def _unpack(self, purpose, subject, token):
        try:
            if not isinstance(token,str) or not 1 <= len(token) <= 8192:
                raise ValueError()
            value = self.service.session_states.protector.unprotect(
                base64.b64decode(token.encode(),altchars=b'-_',validate=True),
                context=f'business-watch:{purpose}:v1:{subject}'.encode())
            return json.loads(value)
        except Exception as exc:
            raise BusinessWatchError('WATCH_REFERENCE_INVALID','来源引用已失效，请重新查询。',status=400) from exc

    def target_reference(self, record, row):
        """Called only on rows retained by the authenticated history service."""
        source = row.get('_source') or {}
        fields = ('affair_id','summary_id','process_id','case_id')
        if any(not isinstance(source.get(k),str) or not re.fullmatch(r'-?\d{1,20}',source[k]) for k in fields):
            return None
        return self._pack('target',record['user_subject'],{
            'version':1,'session_id':record['session_id'],'principal_ref':record['principal_ref'],
            'system_instance':self.system_instance,'expires_at':record['expires_at'],
            'created_at':record['created_at'],
            'source':{**{k:source[k] for k in fields},'title':row['title']},
            'collection':record['query']['collection']})

    def resolve_reference(self, subject, target_ref):
        value = self._unpack('target', subject, target_ref)
        session, principal = self._session(subject)
        try:
            valid = (value['version']==1 and value['session_id']==session['session_id']
                and value['principal_ref']==principal and value['system_instance']==self.system_instance
                and datetime.fromisoformat(value['expires_at']) > _now())
        except (KeyError,TypeError,ValueError):
            valid = False
        if not valid:
            raise BusinessWatchError('WATCH_REFERENCE_EXPIRED','原查询或账号已变化，请重新查询后关注。',status=409)
        return value

    def _observe(self, subject, source, collection, *, principal, background=False, context=None):
        """Only fixed adapter reads; no invoke activity, login card, model or retry."""
        from agentbridge.adapters.seeyon_watch import read_workflow_state
        from agentbridge.adapters.seeyon_central import SeeyonLoginRequired
        from agentbridge.browser.read_budget import read_deadline_scope
        session, _ = self._session(subject, principal=principal)
        trace = None
        if context is None:
            trace,_ = self.service.runtime_governance.ensure_trace(user_subject=subject,
                request_id='watch-check:'+__import__('uuid').uuid4().hex,
                request_kind='read',system_id='oa',capability_name=STATE_CAPABILITY)
        trace_id = context.trace_id if context else trace['trace_id']
        try:
            with read_deadline_scope(time.monotonic()+30), self.service._session_lock(
                session['session_id'],wait_seconds=0.05 if background else 1,
                trace_id=trace_id,purpose='business_watch'):
                current,binding = self._session(subject,principal=principal)
                if (current['session_id'] != session['session_id'] or
                    current.get('authority_generation') != session.get('authority_generation')):
                    raise BusinessWatchError('WATCH_SESSION_CHANGED','OA会话已变化，请稍后重新核对。',status=409)
                state = self.service.session_states.load(current['session_id'])
                if state is None:
                    raise BusinessWatchError('WATCH_SESSION_UNAVAILABLE','OA登录材料不可用，请重新登录。',status=401)
                with ExitStack() as stack:
                    worker = stack.enter_context(self.service.worker_factory(current,self.service.adapter))
                    worker.restore_session_state(state)
                    scope = getattr(self.service.adapter,'capability_scope',None)
                    if callable(scope): stack.enter_context(scope(worker))
                    probe = self.service.adapter.probe_business_draft_session(worker)
                    if (not probe.get('authenticated') or
                        str(probe.get('observed_principal_ref') or '') != current['expected_principal_ref']):
                        raise BusinessWatchError('WATCH_PRINCIPAL_CHANGED','当前OA账号与绑定身份不符，已停止检查。',status=403)
                    snapshot = read_workflow_state(self.service.adapter,worker,source=source,collection=collection)
                    after,_ = self._session(subject,principal=principal)
                    if (after['session_id'] != current['session_id'] or
                        after.get('authority_generation') != current.get('authority_generation')):
                        raise BusinessWatchError('WATCH_SESSION_CHANGED','OA会话已变化，本次结果未保存。',status=409)
                    self.service.user_grants.require(subject,'oa.workflow.read')
                    self.service.session_states.save(current['session_id'],worker.capture_session_state())
            if trace:
                self.service.runtime_governance.update_trace(trace_id,status='succeeded',finished=True)
            return snapshot
        except Exception as exc:
            if trace:
                self.service.runtime_governance.update_trace(trace_id,status='failed',finished=True)
            if isinstance(exc,BusinessWatchError): raise
            if isinstance(exc,PermissionError): raise
            if isinstance(exc,SeeyonLoginRequired):
                self.service.sessions.mark_expired(session['session_id'],'OA登录失效。',source='business_watch')
                raise BusinessWatchError('WATCH_SESSION_UNAVAILABLE','OA登录已失效，请重新登录后恢复关注。',status=401) from exc
            if isinstance(exc,TimeoutError):
                raise BusinessWatchError('WATCH_CHECK_TIMEOUT','检查超时，已保留上次可信结果。',status=504) from exc
            raise BusinessWatchError('WATCH_SOURCE_UNAVAILABLE','本次未能核实流程状态，已保留上次可信结果。',status=409) from exc

    def state(self, subject, target_ref, *, context=None):
        try:
            self.service.user_grants.require(subject,'oa.workflow.read')
            value = self.resolve_reference(subject,target_ref)
            result = self._observe(subject,value['source'],value['collection'],
                principal=value['principal_ref'],context=context)
            return {k:v for k,v in result.items() if k != 'target'}
        except BusinessWatchError as exc:
            if context is not None:
                from agentbridge.core.capability_runtime import CapabilityRejected
                raise CapabilityRejected(exc.code,str(exc)) from exc
            raise

    def create(self, subject, body):
        self.require_permission(subject)
        allowed = {'target_ref','event_types','interval_seconds','expires_in_days','endpoint_ids',
                   'idempotency_key','allow_mark_read'}
        if not isinstance(body,dict) or set(body)-allowed or not body.get('target_ref'):
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请提供当前查询中的具体事项。')
        reference = self.resolve_reference(subject,body['target_ref'])
        allow = body.get('allow_mark_read',False)
        if type(allow) is not bool:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','读取设置不合法。')
        key = body.get('idempotency_key')
        if key is not None and (not isinstance(key,str) or not 1<=len(key.strip())<=200 or '\x00' in key):
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请求标识不合法。')
        days = body.get('expires_in_days',7)
        if type(days) is not int or not 1 <= days <= 30:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','关注期限需要为1至30天。')
        # Validate options/endpoints before any OA navigation.
        interval = body.get('interval_seconds',3600)
        if type(interval) is not int or not 900 <= interval <= 86400:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','检查间隔需要为15分钟至24小时。')
        endpoints = body.get('endpoint_ids',[])
        self._endpoints(subject,endpoints)
        rules = body.get('event_types',['nodes','handlers','ended'])
        if (not isinstance(rules,list) or not rules or any(not isinstance(x,str) for x in rules)
            or len(rules)!=len(set(rules)) or set(rules)-{'nodes','handlers','ended'}):
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请选择需要提醒的变化。')
        if not allow:
            raise BusinessWatchError('WATCH_READ_CONSENT_REQUIRED','检查会打开OA详情，可能标为已读，请先确认读取方式。',status=409)
        snapshot = self._observe(subject,reference['source'],reference['collection'],principal=reference['principal_ref'])
        self.require_permission(subject)
        if snapshot.get('completeness') != 'complete':
            raise BusinessWatchError('WATCH_STATE_INCOMPLETE','当前流程状态未核实完整，暂未建立关注。',status=409)
        watch,reused = self.store.create(user_subject=subject,principal_ref=reference['principal_ref'],
            system_instance=self.system_instance,source={**reference['source'],'collection':reference['collection']},
            title=reference['source']['title'],rules=rules,interval_seconds=interval,
            expires_at=(datetime.fromisoformat(reference['created_at'])+timedelta(days=days)).isoformat(),endpoints=endpoints,
            idempotency_key=body.get('idempotency_key'),allow_mark_read=allow,include_private=True)
        if not reused:
            outcome = self.store.apply_observation(watch['watch_id'],user_subject=subject,
                expected_version=watch['version'],snapshot=snapshot,include_private=True)
            watch = outcome['watch']
        return {'watch':public_watch(watch),'reused':reused}

    def _endpoints(self, subject, endpoints):
        if not isinstance(endpoints,list) or len(endpoints)>8 or any(not isinstance(e,str) for e in endpoints):
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','提醒端点不合法。')
        owned = {e['endpoint_id'] for e in self.service.tasks.list_endpoints(user_subject=subject,active_only=True,limit=500)}
        if set(endpoints)-owned:
            raise BusinessWatchError('WATCH_ENDPOINT_DENIED','只能选择本人已绑定的端点。',status=403)

    def _owned(self, subject, watch_id):
        self.require_permission(subject)
        value = self.store.get(watch_id,user_subject=subject,include_private=True)
        session = self.service.sessions.find(user_subject=subject,system_id='oa')
        if (not session or principal_binding(session)!=value['principal_ref']
            or value['system_instance']!=self.system_instance):
            raise BusinessWatchError('WATCH_PRINCIPAL_CHANGED','OA账号或系统已变化，原关注不可读取。',status=403)
        return value

    def get(self, subject, watch_id):
        value = self._owned(subject,watch_id)
        events = self.store.history(watch_id,user_subject=subject)
        self._owned(subject,watch_id)
        return {'watch':public_watch(value),'events':events}

    def list(self, subject, *, cursor=None, limit=32):
        self.require_permission(subject)
        session,principal = self._session(subject)
        parameters = {'user_subject':subject,'limit':limit,'include_private':True}
        if cursor:
            data = self._unpack('page',subject,cursor)
            if data.get('principal') != principal or data.get('instance') != self.system_instance:
                raise BusinessWatchError('WATCH_REFERENCE_INVALID','账号已变化，请刷新关注列表。')
            parameters.update(before_created_at=data['before_created_at'],before_watch_id=data['before_watch_id'])
        result = self.store.list(**parameters)
        # Old-account watches never become readable after a rebind.
        items = [public_watch(w) for w in result['items'] if w['principal_ref']==principal and w['system_instance']==self.system_instance]
        next_page = result.get('next_cursor')
        token = self._pack('page',subject,{**next_page,'principal':principal,'instance':self.system_instance}) if next_page else None
        self.require_permission(subject)
        self._session(subject,principal=principal)
        return {'items':items,'hasMore':bool(result.get('has_more')),'nextCursor':token}

    def update(self, subject, watch_id, body):
        if not isinstance(body,dict) or set(body)-{'expected_version','action','event_types',
            'interval_seconds','expires_in_days','endpoint_ids','allow_mark_read'}:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','关注设置不合法。')
        action = body.get('action')
        if not isinstance(action,str) or action not in {'pause','resume','stop','configure'}:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请选择暂停、恢复或停止。')
        if type(body.get('expected_version')) is not int or body['expected_version'] < 1:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请提供当前关注版本。')
        if action in {'pause','stop'} and set(body) != {'expected_version','action'}:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','暂停或停止只需要当前版本。')
        value = self.store.get(watch_id,user_subject=subject,include_private=True)
        if action not in {'pause','stop'}:
            self._owned(subject,watch_id)
            self._session(subject,principal=value['principal_ref'])
        kwargs = {'user_subject':subject,'expected_version':body.get('expected_version'),'include_private':True}
        if action in {'pause','resume','stop'}:
            kwargs['state']={'pause':'paused','resume':'active','stop':'stopped'}[action]
        for source,target in [('event_types','rules'),('interval_seconds','interval_seconds'),
                              ('endpoint_ids','endpoints'),('allow_mark_read','allow_mark_read')]:
            if source in body:
                if source=='endpoint_ids': self._endpoints(subject,body[source])
                kwargs[target]=body[source]
        if body.get('allow_mark_read') is False:
            kwargs['state']='paused'
        if 'expires_in_days' in body:
            days=body['expires_in_days']
            if type(days) is not int or not 1<=days<=30:
                raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','关注期限需要为1至30天。')
            kwargs['expires_at']=(_now()+timedelta(days=days)).isoformat()
        updated = self.store.update(watch_id,**kwargs)
        # Stopping remains possible after read permission has been revoked.
        if action in {'pause','stop'}:
            try:
                self._owned(subject,watch_id)
                self._session(subject,principal=value['principal_ref'])
            except (PermissionError,BusinessWatchError):
                return {'watch':{k:updated[k] for k in ('watch_id','state','version')}}
        return {'watch':public_watch(updated),'events':self.store.history(watch_id,user_subject=subject)}

    def check(self, subject, watch_id, *, expected_version):
        if type(expected_version) is not int or expected_version < 1:
            raise BusinessWatchError('WATCH_ARGUMENTS_INVALID','请提供当前关注版本。')
        value = self._owned(subject,watch_id)
        if value['version'] != expected_version:
            raise BusinessWatchError('WATCH_VERSION_CONFLICT','关注已更新，请刷新后再试。',status=409)
        if value['state'] != 'active' or datetime.fromisoformat(value['expires_at']) <= _now():
            raise BusinessWatchError('WATCH_NOT_ACTIVE','关注已暂停或结束，请刷新后查看。',status=409)
        if not value.get('allow_mark_read'):
            raise BusinessWatchError('WATCH_READ_CONSENT_REQUIRED','请先确认允许检查时标为已读。',status=409)
        source = {**value['source'],'title':value['title']}; collection = source.pop('collection')
        snapshot = self._observe(subject,source,collection,principal=value['principal_ref'])
        self.require_permission(subject)
        self._owned(subject,watch_id)
        outcome = self.store.apply_observation(watch_id,user_subject=subject,
            expected_version=expected_version,snapshot=snapshot,include_private=True)
        return {'watch':public_watch(outcome['watch']),'events':self.store.history(watch_id,user_subject=subject)}
