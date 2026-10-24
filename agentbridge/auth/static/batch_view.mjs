// Read-only item states. A checklist never creates navigation or authorization.
export function renderBatchChecklist(document,target,batch) {
  if (!target) return;
  const items = Array.isArray(batch?.items) ? batch.items.slice(0,100).filter(item =>
    Number.isSafeInteger(item?.ordinal) && item.ordinal > 0 && typeof item.state === 'string') : [];
  target.hidden = !items.length;
  if (!items.length) return;
  const labels = {queued:'尚未处理',preparing:'正在准备材料',waiting_user:'等待填写并确认',running:'正在提交与核验',
    succeeded:'已核验完成',failed:'本项未完成',outcome_unknown:'结果待核对',unknown:'结果待核对',
    skipped:'尚未处理',canceled:'已取消',expired:'已过期',superseded:'已被替换'};
  let view = target._batchChecklist;
  if (!view) {
    const root=document.createElement('details'),summary=document.createElement('summary'),list=document.createElement('ol'),note=document.createElement('p');
    root.className='business-batch-list';list.className='business-batch-items';
    note.textContent='每项仍需分别核对并确认。已成功项不会重提；结果待核对项应单独核对，不能当作未处理项跳过。';
    root.append(summary,note,list);target.append(root);view={root,summary,list,rows:new Map()};target._batchChecklist=view;
  }
  const count=Number.isSafeInteger(batch.totalCount) ? batch.totalCount : items.length;
  view.summary.textContent=`本批次清单（${count} 项）`;
  const children=[];
  for(const item of items) {
    let row=view.rows.get(item.ordinal);
    if(!row){row=document.createElement('li');row.style.overflowWrap='anywhere';view.rows.set(item.ordinal,row);}
    const current=item.isCurrent === true;
    row.textContent=`第 ${item.ordinal} 项${current ? ' · 当前项' : ''} · ${String(item.title || `事项 ${item.ordinal}`).slice(0,500)}${item.workflowLabel ? ` · ${String(item.workflowLabel).slice(0,100)}` : ''} · ${labels[item.state] || '状态待核对'}`;
    row.dataset.state=item.state;row.setAttribute('aria-current',current ? 'step' : 'false');children.push(row);
  }
  if(view.list.children.length!==children.length || children.some((node,index)=>view.list.children[index]!==node)) view.list.replaceChildren(...children);
}
