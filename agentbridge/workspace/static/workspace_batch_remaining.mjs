import {isCancellation} from './workspace_lifecycle.mjs';
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function createBatchRemaining({document,state,queryRemaining,getScope,makeKey=()=>crypto.randomUUID()}) {
  state.batchRemaining ||= new Map();
  const make=(tag,text='')=>{const node=document.createElement(tag);node.textContent=text;return node;};
  function update(model) {
    if(model.scope && !model.scope.current()) return;
    const value=model.projection,eligible=value?.available===true && Number.isSafeInteger(value.batchVersion) && value.batchVersion>0 && Number.isSafeInteger(value.remainingCount) && value.remainingCount>0;
    for(const view of model.views) {
      view.root.hidden=!eligible && !model.request && !model.notice;
      view.copy.textContent=`${value?.remainingCount || 0} 项尚未开始办理。重新查询只核对这些原事项的当前资格，已成功项不重提，结果待核对项需单独核对。`;
      view.button.disabled=model.pending || !queryRemaining || !eligible && !model.request;
      view.button.textContent=model.pending ? '正在重新查询剩余事项…' : model.request ? '恢复同一次剩余事项查询' : `重新查询并核对剩余 ${value?.remainingCount || 0} 项`;
      view.status.textContent=model.notice;
    }
  }
  async function submit(model) {
    if(model.pending || !queryRemaining || model.scope && !model.scope.current()) return;
    if(!model.request) {
      if(model.projection?.available!==true || !Number.isSafeInteger(model.projection.batchVersion) || model.projection.batchVersion<1 || !Number.isSafeInteger(model.projection.remainingCount) || !(model.projection.remainingCount>0)) return;
      model.request={requestKey:makeKey(),expectedBatchVersion:model.projection.batchVersion};
    }
    model.pending=true;model.notice='正在重新读取剩余事项；本步骤不会准备或提交业务。';update(model);
    try {
      const result=await queryRemaining(model.taskId,{...model.request});
      if(model.scope && !model.scope.current()) return;
      if(!UUID.test(result?.taskId || '')) throw new Error('查询回执尚未确认，请恢复同一次查询。');
      model.request=null;model.notice='剩余事项查询已受理，请查看新的查询卡；仍需重新选择、核对并逐项确认。';
    } catch(error) {
      if(isCancellation(error)) return;
      model.notice=error.message || '查询回执尚未确认，请恢复同一次查询。';
      if([400,403,404,409,422].includes(error.status)) model.request=null;
    } finally {model.pending=false;update(model);}
  }
  return {render(root,result) {
    const taskId=result.task?.task_id;if(!taskId) return;
    let model=state.batchRemaining.get(taskId);
    if(!model){model={taskId,projection:null,views:new Set(),scope:getScope?.(),request:null,pending:false,notice:''};state.batchRemaining.set(taskId,model);}
    model.projection=result.oaBatchRemaining;
    if(!root._batchRemaining) {
      const copy=make('p'),button=make('button'),status=make('p');button.type='button';button.className='secondary';status.setAttribute('role','status');
      root.className='batch-remaining';root.append(copy,button,status);const view={root,copy,button,status,model};model.views.add(view);root._batchRemaining=view;
      button.addEventListener('click',()=>void submit(model));
    }
    update(model);
  },detach(root){const view=root?._batchRemaining;if(view){view.model.views.delete(view);delete root._batchRemaining;}},
  reset(){for(const model of state.batchRemaining.values())model.views.clear();}};
}
