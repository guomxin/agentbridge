// Complete method configuration stays in the existing draft revision/save flow.
const effects = [{name:'read',description:'读取'}, {name:'reversible_write',description:'可撤销写入'}, {name:'controlled_write',description:'受控写入'}];
const modes = [{name:'read_exploration',description:'读取与文字整理'}, {name:'durable_plan',description:'持久计划'}, {name:'controlled_action',description:'受控业务操作'}];

export function skillConfiguration(bundle) {
  const m=bundle.manifest;
  return {executionMode:m.executionMode, profiles:Object.entries(m.profiles).map(([id,value])=>({id,...structuredClone(value),
    required:[...(m.required_resources?.[id] ?? Object.keys(bundle.resources).filter(name=>name!=='SKILL.md'))]})),
    references:Object.entries(bundle.resources).filter(([name])=>name!=='SKILL.md').map(([path,content])=>({path,content}))};
}

export function configurationProposal(value) {
  if (!modes.some(item=>item.name===value.executionMode)) throw Error('请选择执行方式。');
  if (!value.profiles.length || value.profiles.length>8) throw Error('需要 1 至 8 个模式。');
  if (value.references.length>15) throw Error('最多保存 15 份参考资料。');
  const profiles={}, references={}, required_resources={};
  for (const row of value.references) {
    const path=row.path.trim();
    if (!/^references\/[a-zA-Z0-9_\-\u4e00-\u9fff/ .]+\.md$/u.test(path) || path.includes('..') || path.includes('//')) throw Error('参考资料路径须为 references/名称.md，不能包含上级目录。');
    if (Object.hasOwn(references,path)) throw Error('参考资料路径不能重复。');
    references[path]=row.content;
  }
  for (const row of value.profiles) {
    const id=row.id.trim();
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(id)) throw Error('模式标识须以小写字母开头，最多 32 位，仅含小写字母、数字和下划线。');
    if (Object.hasOwn(profiles,id)) throw Error('模式标识不能重复。');
    const {id:ignored,required,...profile}=row;
    if (profile.description?.length>500) throw Error('模式说明最多 500 字。');
    if (!profile.description?.trim()) delete profile.description;
    else profile.description=profile.description.trim();
    if (profile.execution && !profile.execution.effects.length) throw Error(`请为模式 ${id} 选择至少一种执行效果。`);
    if (profile.execution && value.executionMode==='read_exploration' && profile.execution.effects.some(effect=>effect!=='read')) throw Error('读取与文字整理方式只能选择读取效果；需要写入时请明确调整执行方式。');
    profiles[id]=profile;
    required_resources[id]=[...new Set(required.map(name=>name.trim()))];
    if (required_resources[id].some(path=>!Object.hasOwn(references,path))) throw Error(`模式 ${id} 的必读资料不存在。`);
  }
  return {executionMode:value.executionMode,profiles,references,required_resources};
}

export function createSkillConfigurationEditor({document,root,bundle,catalog={},onChange=()=>{}}) {
  const initial=skillConfiguration(bundle), profiles=[], references=[], inputs=[];
  let disabled=false;
  const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const field=(parent,title,kind,value,{maxLength,rows,choices}={})=>{
    const label=node('label'), titleNode=node('span',title), input=node(kind==='textarea'?'textarea':kind==='select'?'select':'input');
    label.append(titleNode,input);parent.append(label);
    if(choices)for(const item of choices){const option=node('option',item.description);option.value=item.name;input.append(option);}
    if(kind!=='textarea'&&kind!=='select')input.type=kind;
    if(maxLength)input.maxLength=maxLength;if(rows)input.rows=rows;
    if(kind==='checkbox')input.checked=Boolean(value);else input.value=value??'';
    input.dataset.configField=title;inputs.push({input,unavailable:()=>false});
    input.oninput=input.onchange=()=>onChange();
    return input;
  };
  const button=(parent,title,run)=>{const b=node('button',title);b.type='button';b.className='secondary';b.onclick=run;parent.append(b);inputs.push({input:b,unavailable:()=>false});return b;};
  const conditional=(input,unavailable)=>{inputs.find(row=>row.input===input).unavailable=unavailable;};
  const pick=(parent,title,options,selected=[],unavailable=()=>false)=>{
    const details=node('details'), summary=node('summary',title), list=node('div');list.className='skill-config-options';details.append(summary,list);parent.append(details);
    const values=new Map(), known=new Set(options.map(item=>item.name));
    for(const item of [...options,...selected.filter(name=>!known.has(name)).map(name=>({name,description:'未识别：'+name}))]){
      const input=field(list,`${item.description || item.name}（${item.name}）`,'checkbox',selected.includes(item.name));
      input.dataset.configOption=item.name;values.set(item.name,input);conditional(input,unavailable);
    }
    if(!values.size)list.append(node('p','暂无可选项。'));
    return {read:()=>[...values].filter(([,input])=>input.checked).map(([name])=>name),values};
  };
  const setDisabled=value=>{disabled=value;for(const {input,unavailable} of inputs)input.disabled=disabled||unavailable();};
  const changed=()=>{setDisabled(disabled);onChange();};
  root.replaceChildren();
  const mode=field(root,'执行方式','select',initial.executionMode,{choices:modes});
  root.append(node('p',catalog.message || '配置仅限制方法的执行范围，保存和发布不会增加业务权限。'));
  const profileList=node('div'), referenceList=node('div');profileList.dataset.profileList='';referenceList.dataset.referenceList='';
  const addRequired=(profile,reference,selected=false)=>{
    const label=node('label'), input=node('input'), title=node('span',reference.path.value || '未命名资料');
    input.type='checkbox';input.checked=selected;input.onchange=changed;label.append(input,title);profile.requiredRoot.append(label);
    inputs.push({input,unavailable:()=>false});profile.required.set(reference,{input,label,title});
  };
  const addProfile=seed=>{
    if(profiles.length>=8)throw Error('最多添加 8 个模式。');
    const box=node('details'), summary=node('summary',seed?.id || '新模式');box.className='skill-config-card';box.open=!seed;
    const content=node('div');box.append(summary,content);profileList.append(box);
    const row={box,required:new Map()};
    row.id=field(content,'模式标识','text',seed?.id || '',{maxLength:32});
    row.id.oninput=()=>{summary.textContent=row.id.value || '新模式';changed();};
    row.description=field(content,'模式说明','textarea',seed?.description || '',{maxLength:500,rows:2});
    const business=(catalog.business || []).map(item=>({name:item.id,description:item.label}));
    row.all=pick(content,'必须具备全部权限',business,seed?.all || []);
    row.any=pick(content,'至少具备一项权限',business,seed?.any || []);
    row.databaseEnabled=field(content,'需要数据库来源','checkbox',Boolean(seed&&Object.hasOwn(seed,'database')));
    row.databaseEnabled.onchange=changed;
    row.dbAll=pick(content,'数据库必须具备全部能力',catalog.database || [],seed?.database?.all || [],()=>!row.databaseEnabled.checked);
    row.dbAny=pick(content,'数据库至少具备一项能力',catalog.database || [],seed?.database?.any || [],()=>!row.databaseEnabled.checked);
    row.executionEnabled=field(content,'明确设置执行范围','checkbox',!seed || Boolean(seed.execution));
    content.append(node('p','旧模式可沿用原规则；保存新修订时由服务端转换为明确范围。仅需文字处理时不选择业务依赖。'));
    row.executionEnabled.onchange=changed;
    row.effects=pick(content,'允许的执行效果',effects,seed?.execution?.effects || ['read'],()=>!row.executionEnabled.checked);
    row.batch=field(content,'允许批量办理','checkbox',seed?.execution?.allow_batch || false);conditional(row.batch,()=>!row.executionEnabled.checked);
    row.restrict=field(content,'明确选择可用能力','checkbox',Boolean(seed?.execution&&Object.hasOwn(seed.execution,'capabilities')));
    conditional(row.restrict,()=>!row.executionEnabled.checked);row.restrict.onchange=changed;
    content.append(node('p','未明确选择时按最低依赖生成默认范围；明确选择后按本模式需要声明能力，执行时仍须匹配效果、批量限制和当前授权。勾选后不选能力表示不调用业务能力。'));
    row.capabilities=pick(content,'限定的能力',catalog.capabilities || [],seed?.execution?.capabilities || [],()=>!row.executionEnabled.checked||!row.restrict.checked);
    row.requiredRoot=node('fieldset');row.requiredRoot.className='skill-config-required';row.requiredRoot.append(node('legend','本模式必读资料'));content.append(row.requiredRoot);
    for(const reference of references)addRequired(row,reference,(seed?.required || []).includes(reference.path.value));
    button(content,'删除模式',()=>{profiles.splice(profiles.indexOf(row),1);box.remove();changed();});
    profiles.push(row);setDisabled(disabled);return row;
  };
  const addReference=seed=>{
    if(references.length>=15)throw Error('最多添加 15 份参考资料。');
    const box=node('details'),summary=node('summary',seed?.path || '新参考资料');box.className='skill-config-card';box.open=!seed;
    const content=node('div');box.append(summary,content);referenceList.append(box);
    const row={box,path:field(content,'资料路径','text',seed?.path || 'references/',{maxLength:256}),
      content:field(content,'资料内容','textarea',seed?.content || '',{maxLength:40000,rows:8})};
    row.content.className='skill-config-text';
    row.path.oninput=()=>{summary.textContent=row.path.value || '新参考资料';for(const profile of profiles){const required=profile.required.get(row);if(required)required.title.textContent=row.path.value||'未命名资料';}changed();};
    button(content,'删除资料',()=>{references.splice(references.indexOf(row),1);box.remove();for(const profile of profiles){profile.required.get(row)?.label.remove();profile.required.delete(row);}changed();});
    references.push(row);for(const profile of profiles)addRequired(profile,row);setDisabled(disabled);return row;
  };
  root.append(node('h4','参考资料'),node('p','新资料默认为可选。请在需要的模式中勾选必读；主说明始终加载。'),referenceList);
  const message=node('p');message.setAttribute('role','status');
  const safely=run=>()=>{try{run();message.textContent='';changed();}catch(error){message.textContent=error.message;}};
  button(root,'添加参考资料',safely(()=>addReference()));
  root.append(node('h4','使用模式'),profileList);
  button(root,'添加模式',safely(()=>addProfile()));root.append(message);
  for(const seed of initial.references)addReference(seed);
  for(const seed of initial.profiles)addProfile(seed);
  return {
    read:()=>({executionMode:mode.value,
      references:references.map(row=>({path:row.path.value,content:row.content.value})),
      profiles:profiles.map(row=>({id:row.id.value,description:row.description.value,all:row.all.read(),any:row.any.read(),
        ...(row.databaseEnabled.checked?{database:{all:row.dbAll.read(),any:row.dbAny.read()}}:{}),
        ...(row.executionEnabled.checked?{execution:{effects:row.effects.read(),allow_batch:row.batch.checked,
          ...(row.restrict.checked?{capabilities:row.capabilities.read()}:{})}}:{}),
        required:[...row.required].filter(([,item])=>item.input.checked).map(([reference])=>reference.path.value)}))}),
    setDisabled,
  };
}
