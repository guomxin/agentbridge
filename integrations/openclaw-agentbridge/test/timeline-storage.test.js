import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,statSync,symlinkSync,chmodSync,mkdirSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {TimelineLocalStore,openTimelineLocalStore} from '../lib/timeline-storage.js';

function temporary(t){const dir=mkdtempSync(path.join(os.tmpdir(),'agentbridge-timeline-storage-'));t.after(()=>rmSync(dir,{recursive:true,force:true}));return dir;}

test('third-party runtime opens only its own private database with FULL synchronization',t=>{
  const home=temporary(t);let forbiddenCalls=0;
  const store=openTimelineLocalStore({runtime:{state:{resolveStateDir:()=>home,openSyncKeyedStore(){forbiddenCalls++;throw Error('Trusted official plugins only');}}}});
  t.after(()=>store.close());store.registerIfAbsent('message',{text:'synthetic public text'});
  assert.equal(forbiddenCalls,0);assert.equal(store.db.prepare('PRAGMA synchronous').get().synchronous,2);
  assert.equal(statSync(store.filename).mode&0o077,0);assert.equal(statSync(path.dirname(store.filename)).mode&0o077,0);
  assert.equal(openTimelineLocalStore({}),null);
});

test('two independent database handles serialize register-if-absent and updates',t=>{
  const home=temporary(t),first=new TimelineLocalStore(home),second=new TimelineLocalStore(home);t.after(()=>{first.close();second.close();});
  assert.equal(first.registerIfAbsent('original',{count:0}),true);assert.equal(second.registerIfAbsent('original',{count:99}),false);
  second.update('original',value=>({count:value.count+1}));assert.equal(first.lookup('original').count,1);
  assert.throws(()=>first.update('original',()=>{throw Error('INTERRUPTED');}),/INTERRUPTED/);assert.equal(second.lookup('original').count,1);
});

test('namespace rejects new records at capacity without evicting pending or receipts',t=>{
  const store=new TimelineLocalStore(temporary(t),{maxEntries:2});t.after(()=>store.close());
  store.registerIfAbsent('pending',{state:'pending'});store.registerIfAbsent('receipt',{state:'sent'});
  assert.throws(()=>store.registerIfAbsent('another',{}),/CAPACITY/);assert.equal(store.entries().length,2);
  store.update('pending',value=>({...value,attempts:1}));assert.equal(store.lookup('pending').attempts,1);
});

test('bounded values refuse overlarge writes atomically',t=>{
  const store=new TimelineLocalStore(temporary(t));t.after(()=>store.close());
  assert.throws(()=>store.registerIfAbsent('too-big',{text:'x'.repeat(65536)}),/TOO_LARGE/);assert.equal(store.entries().length,0);
});

test('symlink database and broad directory permissions are refused',t=>{
  const home=temporary(t),target=path.join(home,'target');mkdirSync(target,{mode:0o700});
  const broad=path.join(home,'broad');mkdirSync(broad,{mode:0o755});chmodSync(broad,0o755);
  assert.throws(()=>new TimelineLocalStore(broad),/PRIVATE_STORAGE/);
  symlinkSync(path.join(home,'elsewhere'),path.join(target,'projection.sqlite'));
  assert.throws(()=>new TimelineLocalStore(target),/PRIVATE_STORAGE/);
});

test('committed rows survive a separate process exiting without an explicit close',t=>{
  const home=temporary(t),module=new URL('../lib/timeline-storage.js',import.meta.url).href;
  execFileSync(process.execPath,['--input-type=module','-e',`import {TimelineLocalStore} from ${JSON.stringify(module)};const s=new TimelineLocalStore(${JSON.stringify(home)});s.registerIfAbsent('pending',{messageKey:'synthetic-original',state:'pending'});process.exit(0);`]);
  const store=new TimelineLocalStore(home);t.after(()=>store.close());assert.equal(store.lookup('pending').messageKey,'synthetic-original');
});
