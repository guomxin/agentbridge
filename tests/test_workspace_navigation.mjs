import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaceViewState,createReadingScroll,createPreparationHandoff} from '../agentbridge/workspace/static/workspace_navigation.mjs';
import {createLifecycle} from '../agentbridge/workspace/static/workspace_lifecycle.mjs';
import {createDocument} from './workspace_dom_fixture.mjs';
const flush = () => new Promise(resolve => setImmediate(resolve));

test('view preferences isolate accounts and sources, expire, and never persist action or content fields',() => {
  const data = new Map(), storage = {getItem:key=>data.get(key),setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};
  let account = 'a', time = 100;
  const views = createWorkspaceViewState({storage,getAccountId:()=>account,now:()=>time});
  views.setSource('source:operation',{search:'周报',type:'周报阅办',selected:['one'],onlySelected:true,values:{opinion:'secret'},flowUrl:'secret',requestKey:'secret'});
  views.saveReading({top:120,atBottom:false,anchor:'task:source',offset:20,token:'secret'});
  assert.doesNotMatch([...data.values()].join(''),/secret|flowUrl|requestKey|values|token/);
  assert.equal(views.getSource('different').search,'');
  account='b'; assert.equal(views.reading(),null); assert.deepEqual(views.getSource('source:operation').selected,[]);
  account='a'; assert.equal(views.reading().top,120); assert.equal(views.getSource('source:operation').search,'周报');
  time+=86400001; assert.equal(views.reading(),null);
  views.setSource('source',{search:'x'});views.clear();assert.equal(data.size,0);
});

test('incoming updates preserve reading position and show a hint; own send or hint click follows latest',() => {
  const document=createDocument(),container=document.createElement('div'),notice=document.createElement('button');
  Object.assign(container,{scrollTop:600,scrollHeight:1000,clientHeight:400});
  const scroll=createReadingScroll({container,notice});
  container.events.scroll(); container.scrollHeight=1300;scroll.update();assert.equal(container.scrollTop,1300);
  container.scrollTop=200;container.events.scroll();container.scrollHeight=1500;scroll.update();
  assert.equal(container.scrollTop,200);assert.equal(notice.hidden,false);
  notice.events.click();assert.equal(container.scrollTop,1500);assert.equal(notice.hidden,true);
  scroll.restore({top:170,atBottom:false});scroll.update();assert.equal(container.scrollTop,170);
  scroll.update({force:true});assert.equal(container.scrollTop,1500);
});

test('reading restore follows a stable item when preceding messages changed height',() => {
  const document=createDocument(),container=document.createElement('div'),notice=document.createElement('button'),item=document.createElement('article');
  item.dataset.timelineKey='source';item.offsetTop=300;container.append(item);
  Object.assign(container,{scrollTop:350,scrollHeight:2000,clientHeight:400});
  const scroll=createReadingScroll({container,notice});container.events.scroll();const saved=scroll.snapshot();
  item.offsetTop=500;scroll.restore(saved);assert.equal(container.scrollTop,550);
});

function handoffFixture() {
  const timers=new Map();let sequence=0;
  const lifecycle=createLifecycle({timers:{setTimeout:(fn)=>{timers.set(++sequence,fn);return sequence;},clearTimeout:id=>timers.delete(id)}});
  const scope=lifecycle.scope(),navigations=[],results=[],progress=[],reads=[];let detail;
  const handoff=createPreparationHandoff({getScope:()=>scope,read:async id=>{reads.push(id);return detail;},
    navigate:url=>navigations.push(url),openResult:id=>results.push(id)});
  return {handoff,scope,navigations,results,progress,reads,set:next=>{detail=next;},
    async tick(){const [id,fn]=[...timers].at(0);timers.delete(id);fn();await flush();},timers,
    start(options={}){return handoff.start('child',{onProgress:(...args)=>progress.push(args),...options});}};
}
const waiting=()=>({task:{task_id:'child',status:'waiting_user'},interactions:[{interactionId:'current',state:'pending',type:'input',presentation:{url:'https://trusted.example/flow/input/card'}}]});
test('explicit preparation polls read-only then opens only the unique current trusted form',async()=>{
  const f=handoffFixture();f.set({task:{task_id:'child',status:'running'}});f.start({automatic:true});await flush();
  assert.match(f.progress.at(-1)[0],/正在准备材料/);assert.deepEqual(f.navigations,[]);
  f.set(waiting());await f.tick();assert.deepEqual(f.navigations,['https://trusted.example/flow/input/card']);assert.equal(f.timers.size,0);
});
test('unknown and terminal results never auto-open even when stale pending links survive',async()=>{
  for(const status of ['outcome_unknown','succeeded','failed','expired']) {
    const f=handoffFixture();f.set({...waiting(),task:{task_id:'child',status}});f.start({automatic:true});await flush();
    assert.deepEqual(f.navigations,[]);assert.deepEqual(f.results,[]);assert.equal(f.timers.size,0);
    f.start();await flush();assert.deepEqual(f.results,['child']);
  }
});
test('ambiguous forms, noncurrent batch items and invalid destinations cannot auto-open',async()=>{
  for(const detail of [{...waiting(),interactions:[...waiting().interactions,{...waiting().interactions[0],interactionId:'other'}]},
    {...waiting(),batch:{state:'waiting_user',currentOrdinal:2,items:[{ordinal:1,interactionId:'current'}]}},
    {...waiting(),interactions:[{...waiting().interactions[0],presentation:{url:'https://u:p@other.test/flow/input/card'}}]}]) {
    const f=handoffFixture();f.set(detail);f.start();await flush();assert.deepEqual(f.navigations,[]);f.handoff.cancel();
  }
});
test('cancellation and account disposal discard delayed reads without navigation',async()=>{
  let release;const pending=new Promise(resolve=>{release=resolve;});
  const lifecycle=createLifecycle(),scope=lifecycle.scope(),navigations=[];
  const handoff=createPreparationHandoff({getScope:()=>scope,read:()=>pending,navigate:url=>navigations.push(url)});
  handoff.start('child');scope.dispose();release(waiting());await flush();assert.deepEqual(navigations,[]);
  const f=handoffFixture();f.set({task:{task_id:'child',status:'running'}});const cancel=f.start();await flush();cancel();
  assert.equal(f.timers.size,0);assert.equal(f.progress.at(-1)[1],false);
});

test('authentication failure stops automatic waiting without opening a form',async()=>{
  const f=handoffFixture();f.set({task:{task_id:'child',status:'active'},
    preparationHealth:{state:'authentication_required',message:'连接认证失败，请续期。'}});
  f.start({automatic:true});await flush();
  assert.deepEqual(f.navigations,[]);assert.equal(f.timers.size,0);
});
