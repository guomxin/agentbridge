const $ = (selector) => document.querySelector(selector);
const target = location.pathname.match(/^\/database\/logs\/([a-z][a-z0-9_-]{0,63})\/([0-9]{1,19})$/);
const expectedRevision = new URLSearchParams(location.search).get('revision');
const errorMessages = {
  DATABASE_LOG_NOT_FOUND: '这条日志不存在或已删除。',
  DATABASE_CAPABILITY_DENIED: '当前账号没有此数据源的日志原文读取权限。请联系管理员确认授权。',
  DATABASE_AUTHORIZATION_CHANGED: '数据库授权已变化，请重新登录或确认权限后重试。',
};
function status(message) { $('#status').hidden = false; $('#status').textContent = message; }
function metadata(label, value) {
  const item = document.createElement('div');
  const term = document.createElement('dt'); term.textContent = label;
  const detail = document.createElement('dd'); detail.textContent = value || '未提供';
  item.append(term, detail); $('#metadata').append(item);
}
async function load() {
  $('#record').hidden = true; $('#auth').hidden = true; $('#retry').hidden = true;
  $('#metadata').replaceChildren(); $('#paragraphs').replaceChildren();
  $('#title').textContent = '日志原文'; document.title = '日志原文 · AgentBridge';
  status('正在读取日志……');
  if (!target) { status('原文链接无效。'); return; }
  try {
    const response = await fetch(`/api/database/logs/${target[1]}/${target[2]}`, {cache:'no-store'});
    const data = await response.json();
    if (response.status === 401) { status('请登录后查看。'); $('#auth').hidden = false; return; }
    if (!response.ok) { status(errorMessages[data.error?.code] || '暂时无法读取日志，请稍后重试。'); $('#retry').hidden = false; return; }
    $('#status').hidden = true;
    $('#title').textContent = `${data.author || '未署名'}的${data.log_type === 'WEEKLY' ? '周报' : '日报'}`;
    document.title = `${data.author || '未署名'} · ${data.log_date} · 日志原文`;
    $('#metadata').replaceChildren();
    metadata('日志日期', data.log_date); metadata('作者', data.author); metadata('当前部门', data.department);
    metadata('来源', data.source_name || '日志数据库'); metadata('更新时间', data.updated_at); metadata('读取时间', new Date(data.queried_at).toLocaleString('zh-CN'));
    const changed = expectedRevision && expectedRevision !== data.revision;
    $('#changed').hidden = !changed;
    $('#changed').textContent = '这条日志的正文在引用后已变化。以下显示当前内容，原引用的段落位置可能已变化。';
    $('#paragraphs').replaceChildren();
    for (const paragraph of data.paragraphs) {
      const element = document.createElement('p'); element.className = 'paragraph';
      element.id = `paragraph-${paragraph.number}`; element.textContent = paragraph.text || '\u00a0';
      $('#paragraphs').append(element);
    }
    $('#record').hidden = false;
    if (!changed && /^#paragraph-\d+$/.test(location.hash)) {
      const focus = document.getElementById(location.hash.slice(1));
      if (focus) { focus.classList.add('highlight'); focus.scrollIntoView({block:'center'}); }
    }
  } catch { status('网络连接失败，请重试。'); $('#retry').hidden = false; }
}
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('button'); button.disabled = true;
  try {
    const values = new FormData(form);
    const response = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:values.get('username'), password:values.get('password')})});
    form.elements.password.value = '';
    if (!response.ok) { status('登录失败，请检查账号与密码后重试。'); return; }
    await load();
  } catch { status('登录请求失败，请稍后重试。'); }
  finally { button.disabled = false; }
});
$('#retry').addEventListener('click', load);
load();
