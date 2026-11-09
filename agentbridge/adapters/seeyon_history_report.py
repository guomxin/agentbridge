"""Bounded, read-only history pages and current workflow diagram evidence.

Only registered list/summary/diagram routes are used. Private row identities are
never accepted from a model: the central query store owns the source rows.
"""
from __future__ import annotations

from datetime import datetime
import re
from urllib.parse import urlencode, urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET

from agentbridge.browser.read_budget import remaining_seconds, remaining_ms, check_read_deadline
from agentbridge.adapters.seeyon_central import SeeyonReadContractMismatch, SeeyonLoginRequired

PAGE_SIZE = 50
MAX_XML_CHARS = 1_000_000

_PAGE_SCRIPT = r"""
async ({method, filters, pageNumber, timeoutMs}) => {
  if (typeof window.colManager !== 'function' || typeof window.CallerResponder !== 'function')
    throw new Error('OA history contract unavailable');
  const manager = new window.colManager();
  const p = await new Promise((resolve,reject) => {
    const timer=setTimeout(()=>reject(new Error('OA history read timed out')),timeoutMs);
    const cb=new window.CallerResponder();
    cb.success=x=>{clearTimeout(timer);resolve(x)};
    cb.error=()=>{clearTimeout(timer);reject(new Error('OA history read failed'))};
    try { manager[method]({page:pageNumber,size:50},filters,cb); }
    catch(e){clearTimeout(timer);reject(e)}
  });
  if(!p || !Array.isArray(p.data)) throw new Error('Invalid history rows');
  const keys=['affairId','subject','startDate','createDate','dealTime','currentNodesInfo',
    'flowFinished','state','summaryState','subState','category','startMemberName',
    'processId','summaryId','caseId','templeteId','affairNodeName'];
  return {total:p.total,pages:p.pages,page:p.page,size:p.size,
    items:p.data.map(row=>Object.fromEntries(keys.map(k=>[k,row[k]??null])))};
}
"""

_DIAGRAM_SCRIPT = r"""
() => ({process_id:String(window.processId||''),case_id:String(window.caseId||''),
  process_xml:String(window.initialize_processXml||''),
  case_log_xml:String(window.initialize_caseLogXml||''),
  workitem_log_xml:String(window.initialize_caseWorkitemLogXml||''),
  nodes:Array.from(document.querySelectorAll('g.wf_node[node_id]')).map(n=>({
    id:n.getAttribute('node_id'),
    icons:Array.from(n.querySelectorAll('use')).map(u=>u.getAttribute('href')||u.getAttribute('xlink:href')||'')
  }))})
"""

_ID = re.compile(r"^-?\d{1,20}$")


def _text(value, maximum=500):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SeeyonReadContractMismatch('OA history identity is invalid')
    return value


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", value):
        return None
    try:
        return datetime.fromisoformat(value).isoformat(timespec='seconds') + '+08:00'
    except ValueError:
        return None


def _same_route(actual, expected):
    current, target = urlparse(actual), urlparse(expected)
    return (current.scheme, current.netloc, current.path) == (target.scheme, target.netloc, target.path) and all(
        parse_qs(current.query).get(key) == values for key, values in parse_qs(target.query).items())


def public_history_row(raw, collection):
    if collection not in {'done', 'sent'} or not isinstance(raw, dict):
        raise SeeyonReadContractMismatch('Invalid history collection or row')
    affair_id = _id(raw.get('affairId'))
    title = _text(raw.get('subject'), 1000)
    if not title:
        raise SeeyonReadContractMismatch('OA history row has no title')
    initiated, processed = _date(raw.get('startDate')), _date(raw.get('dealTime'))
    finished = raw.get('flowFinished')
    status = _text(raw.get('currentNodesInfo'), 1000)
    # flowFinished proves the flow ended, not that payment or every business
    # objective succeeded. nodeName/affairNodeName belong to the user's OLD node.
    state = 'ended' if finished is True else 'in_progress' if finished is False else 'unknown'
    unavailable = []
    if not initiated: unavailable.append('initiated_at')
    if collection == 'done' and not processed: unavailable.append('processed_at')
    if state == 'unknown': unavailable.append('workflow_state')
    if finished is not True: unavailable.append('current_nodes')
    handlers = [] if finished is True else [_text(x, 120) for x in re.split(r'[、,，;；\n]+',status) if _text(x)]
    if finished is not True and not handlers: unavailable.append('current_handlers')
    source = {'affair_id': affair_id, 'title': title,
              'process_id': raw.get('processId'), 'summary_id': raw.get('summaryId'),
              'case_id': raw.get('caseId'), 'flow_finished': finished}
    return {'affair_id':affair_id,'title':title,'category':_text(raw.get('category'),120),
            'initiated_at':initiated,'processed_at':processed,
            'workflow_state':state,'workflow_state_text':'已结束' if finished is True else '流程未结束' if finished is False else '状态未核实',
            'current_handlers':handlers,'current_nodes':[] if finished is True else None,
            'observed_at':datetime.now().astimezone().isoformat(),
            'unavailable_fields':unavailable,'_source':source,
            '_keyword_fields':{'title':title,'date':processed if collection=='done' else initiated,
                               'status':status,'category':_text(raw.get('category'),120),'affair_id':affair_id}}


def read_report_page(adapter, worker, collection, page_number, temporal):
    """Read one proven page; the service persists, validates and bounds traversal."""
    if collection not in {'done','sent'} or type(page_number) is not int or page_number < 1:
        raise ValueError('Invalid history page')
    method = 'getDoneList' if collection == 'done' else 'getSentList'
    route = urljoin(adapter.base_url, 'collaboration/collaboration.do?' + urlencode({'method':'listDone' if collection=='done' else 'listSent'}))
    if not _same_route(worker.page_url, route):
        worker.goto(route, timeout_seconds=remaining_seconds(12))
    page = worker.page
    if 'login' in worker.page_url.lower():
        raise SeeyonLoginRequired('OA session expired while reading history')
    if not _same_route(worker.page_url, route):
        raise SeeyonReadContractMismatch('OA history list route changed')
    page.wait_for_function("method=>typeof window.colManager==='function' && typeof window.CallerResponder==='function' && typeof (new window.colManager())[method]==='function'",arg=method,timeout=remaining_ms(8000))
    basis = temporal['dateBasis']
    bounds = temporal['absoluteRange']
    server_filter = basis == ('processed_at' if collection=='done' else 'initiated_at')
    filters = {'dumpData':'false'}
    if collection=='done':filters.update(deduplication='false',aiProcessing='false')
    if server_filter and (bounds['start'] or bounds['end']):
        filters['dealDate' if collection=='done' else 'createDate'] = f"{bounds['start'] or ''}#{bounds['end'] or ''}"
    result=page.evaluate(_PAGE_SCRIPT,{'method':method,'filters':filters,'pageNumber':page_number,'timeoutMs':remaining_ms(8000)})
    check_read_deadline()
    if not _same_route(worker.page_url, route):
        raise SeeyonReadContractMismatch('OA history list route changed during the read')
    if not isinstance(result,dict) or not isinstance(result.get('items'),list):
        raise SeeyonReadContractMismatch('Invalid OA history response')
    result['items']=[public_history_row(raw,collection) for raw in result['items']]
    result['server_filter_applied']=server_filter and bool(bounds['start'] or bounds['end'])
    return result


def _xml(text, root_tag):
    if not isinstance(text,str) or len(text)>MAX_XML_CHARS or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise SeeyonReadContractMismatch('Unsafe or oversized workflow XML')
    try:root=ET.fromstring(text)
    except ET.ParseError as exc:raise SeeyonReadContractMismatch('Invalid workflow XML') from exc
    if root.tag!=root_tag:raise SeeyonReadContractMismatch('Unexpected workflow XML contract')
    return root


def parse_current_nodes(snapshot, source):
    """Join authoritative latest node state, assigned people and rendered glyphs.

    S=2 is validated against the diagram's active unread/read marker. Completed
    personal nodes (even highlighted as current selection) never count as active.
    """
    if not isinstance(snapshot,dict) or snapshot.get('process_id')!=_id(source.get('process_id')) or snapshot.get('case_id')!=_id(source.get('case_id')):
        raise SeeyonReadContractMismatch('Workflow diagram identity changed')
    process=_xml(snapshot.get('process_xml'),'ps')
    processes=process.findall('p')
    if len(processes)!=1 or processes[0].get('i')!=source['process_id']:
        raise SeeyonReadContractMismatch('Workflow process identity changed')
    nodes={}
    for node in processes[0].findall('n'):
        identifier=node.get('i')
        if not identifier or identifier in nodes:raise SeeyonReadContractMismatch('Ambiguous workflow nodes')
        nodes[identifier]=node
    states={}
    for step in _xml(snapshot.get('case_log_xml'),'caseLog').findall('S'):
        for entry in step.findall('F'):states[entry.get('N')]=entry.get('S')
    workitems=_xml(snapshot.get('workitem_log_xml'),'caseItemLog').findall('WL')
    rendered={}
    for node in snapshot.get('nodes',[]):
        if not isinstance(node,dict) or not node.get('id') or node['id'] in rendered:
            raise SeeyonReadContractMismatch('Ambiguous rendered workflow nodes')
        rendered[node['id']]=node.get('icons',[])
    result=[]
    for identifier,state in states.items():
        node=nodes.get(identifier)
        if state!='2':continue
        if node is None:raise SeeyonReadContractMismatch('Active node is missing from the process')
        if node.get('t') in {'2','3','4','8'}:continue
        if node.get('t')!='6':raise SeeyonReadContractMismatch('Unsupported active workflow node type')
        icons=rendered.get(identifier,[])
        if not any(icon in {'#icon_nodestate_unread_','#icon_nodestate_read_'} for icon in icons):
            raise SeeyonReadContractMismatch('Active workflow node is not proven by the rendered diagram')
        handlers=[]
        for item in workitems:
            if item.get('N')!=identifier:continue
            actions=item.get('AS','').split(',')
            # Only the observed pending/read histories are accepted; changed or
            # unfamiliar action histories stay unknown rather than guessed.
            if not actions or any(action not in {'0','26'} for action in actions):continue
            name=_text(item.get('PN'),120)
            if name and name not in handlers:handlers.append(name)
        name=_text(node.get('n'),300)
        if not name or not handlers:raise SeeyonReadContractMismatch('Current node or assignee is unavailable')
        result.append({'name':name,'handlers':handlers})
    if not result:raise SeeyonReadContractMismatch('No active workflow node could be verified')
    return result


def read_current_nodes(adapter, worker, collection, row):
    source=dict(row['_source'])
    for name in ('affair_id','process_id','summary_id','case_id'):_id(source.get(name))
    route=urljoin(adapter.base_url,'collaboration/collaboration.do?'+urlencode({'method':'summary','openFrom':'listDone' if collection=='done' else 'listSent','affairId':source['affair_id'],'showTab':'true'}))
    page=worker.goto(route,timeout_seconds=remaining_seconds(12))
    if 'login' in worker.page_url.lower():raise SeeyonLoginRequired('OA session expired while reading workflow status')
    if not _same_route(worker.page_url, route):raise SeeyonReadContractMismatch('Workflow detail route changed')
    page.wait_for_function("() => typeof window.summaryId!=='undefined' && typeof window.refreshWorkflow==='function'",timeout=remaining_ms(8000))
    identity=page.evaluate("() => ({affair_id:String(window.affairId||''),summary_id:String(window.summaryId||''),process_id:String(window._summaryProcessId||''),case_id:String(window._summaryCaseId||''),finished:window.isFinish===true||window.isFinish==='true'})")
    if any(identity.get(k)!=source[k] for k in ('affair_id','summary_id','process_id','case_id')):
        raise SeeyonReadContractMismatch('Workflow detail identity changed or access is unavailable')
    if identity['finished']:
        return {'current_nodes':[],'current_handlers':[],'workflow_state':'ended','workflow_state_text':'已结束','observed_at':datetime.now().astimezone().isoformat(),'unavailable_fields':[f for f in row['unavailable_fields'] if f not in {'current_nodes','current_handlers','workflow_state'}]}
    page.locator('#workflow_view_li a').click(timeout=remaining_ms(5000))
    frame=page.frame_locator('#iframeright')
    frame.locator('g.wf_node[node_id]').first.wait_for(state='attached',timeout=remaining_ms(8000))
    candidates=[f for f in page.frames if urlparse(f.url).path==urlparse(urljoin(adapter.base_url,'workflow/designer.do')).path]
    if len(candidates)!=1 or not _same_route(candidates[0].url,urljoin(adapter.base_url,'workflow/designer.do?method=showDiagram')):
        raise SeeyonReadContractMismatch('Workflow diagram route is not authoritative')
    snapshot=candidates[0].evaluate(_DIAGRAM_SCRIPT)
    nodes=parse_current_nodes(snapshot,source)
    handlers=list(dict.fromkeys(person for n in nodes for person in n['handlers']))
    return {'current_nodes':nodes,'current_handlers':handlers,'workflow_state':'in_progress','workflow_state_text':'处理中','observed_at':datetime.now().astimezone().isoformat(),'unavailable_fields':[f for f in row['unavailable_fields'] if f not in {'current_nodes','current_handlers','workflow_state'}]}
