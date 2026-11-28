import {DatabaseSync} from 'node:sqlite';
import {chmodSync,closeSync,fsyncSync,lstatSync,mkdirSync,openSync} from 'node:fs';
import path from 'node:path';

const opened=new Map();
const MAX_VALUE_BYTES=64*1024;
function syncDirectory(directory) {
  const fd=openSync(directory,'r');try {fsyncSync(fd);} finally {closeSync(fd);}
}
function privateRegular(file) {
  let stat;
  try {stat=lstatSync(file);} catch(error) {if(error.code==='ENOENT')return;throw error;}
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink!==1 || stat.mode&0o077 ||
      typeof process.getuid==='function' && stat.uid!==process.getuid()) throw new Error('TIMELINE_PRIVATE_STORAGE_REQUIRED');
}
function encoded(value) {
  const json=JSON.stringify(value);
  if (typeof json!=='string' || Buffer.byteLength(json,'utf8')>MAX_VALUE_BYTES) throw new Error('TIMELINE_STORE_VALUE_TOO_LARGE');
  return json;
}

// This database belongs solely to AgentBridge. Third-party plugins are not
// permitted to open the SDK's official-plugin state stores in OpenClaw 2026.7.1.
// No SDK database, trust flag, global PRAGMA or Telegram offset is changed.
export class TimelineLocalStore {
  constructor(directory,{maxEntries=10_000}={}) {
    if (!path.isAbsolute(directory) || !Number.isSafeInteger(maxEntries) || maxEntries<2) throw new Error('TIMELINE_STORAGE_CONFIG_INVALID');
    const first=mkdirSync(directory,{recursive:true,mode:0o700});
    const stat=lstatSync(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink() || stat.mode&0o077 ||
        typeof process.getuid==='function' && stat.uid!==process.getuid()) throw new Error('TIMELINE_PRIVATE_STORAGE_REQUIRED');
    if (first) {
      let current=directory;
      for (;;) {syncDirectory(current);if(current===path.dirname(first))break;current=path.dirname(current);}
    }
    this.directory=directory;
    this.filename=path.join(directory,'projection.sqlite');this.maxEntries=maxEntries;
    for(const suffix of ['', '-wal', '-shm', '-journal'])privateRegular(this.filename+suffix);
    const db=new DatabaseSync(this.filename);
    try {
      chmodSync(this.filename,0o600);
      db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=2000;');
      db.exec('CREATE TABLE IF NOT EXISTS timeline_projection_entries(key TEXT PRIMARY KEY,value TEXT NOT NULL)');
      for(const suffix of ['', '-wal', '-shm'])privateRegular(this.filename+suffix);
      syncDirectory(directory);
      if(db.prepare('PRAGMA synchronous').get().synchronous!==2)throw new Error('TIMELINE_FULL_SYNC_REQUIRED');
      this.db=db;
    } catch(error) {db.close();throw error;}
  }
  transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try {const result=fn();this.db.exec('COMMIT');return result;}
    catch(error){try{this.db.exec('ROLLBACK');}catch{}throw error;}
  }
  lookup(key) {
    const row=this.db.prepare('SELECT value FROM timeline_projection_entries WHERE key=?').get(key);
    return row ? JSON.parse(row.value) : undefined;
  }
  capacity(key) {
    if (!this.db.prepare('SELECT 1 FROM timeline_projection_entries WHERE key=?').get(key) &&
        this.db.prepare('SELECT count(*) AS count FROM timeline_projection_entries').get().count>=this.maxEntries) {
      throw new Error('TIMELINE_STORE_CAPACITY_EXCEEDED');
    }
  }
  registerIfAbsent(key,value) {
    const json=encoded(value);
    return this.transaction(()=>{
      if(this.lookup(key)!==undefined)return false;
      this.capacity(key);this.db.prepare('INSERT INTO timeline_projection_entries VALUES(?,?)').run(key,json);return true;
    });
  }
  update(key,fn) {
    return this.transaction(()=>{
      const next=fn(this.lookup(key));if(next===undefined)return false;
      const json=encoded(next);this.capacity(key);
      this.db.prepare('INSERT INTO timeline_projection_entries VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(key,json);return true;
    });
  }
  delete(key) {return this.transaction(()=>this.db.prepare('DELETE FROM timeline_projection_entries WHERE key=?').run(key).changes>0);}
  entries() {return this.db.prepare('SELECT key,value FROM timeline_projection_entries ORDER BY key').all().map(row=>({key:row.key,value:JSON.parse(row.value)}));}
  entriesWithPrefix(prefix) {return this.db.prepare('SELECT key,value FROM timeline_projection_entries WHERE substr(key,1,?)=? ORDER BY key')
    .all(prefix.length,prefix).map(row=>({key:row.key,value:JSON.parse(row.value)}));}
  keysWithPrefix(prefix) {return this.db.prepare('SELECT key FROM timeline_projection_entries WHERE substr(key,1,?)=? ORDER BY key')
    .all(prefix.length,prefix).map(row=>row.key);}
  close() {
    if (opened.get(this.directory) === this) opened.delete(this.directory);
    this.db.close();
  }
}

export function compactCompletedEntries(store, candidates, { now = Date.now(), retentionMs = 30 * 86400_000 } = {}) {
  if (!store || typeof store.delete !== 'function' || !Array.isArray(candidates) || !candidates.length) return 0;
  const maxEntries = Number.isSafeInteger(store.maxEntries) && store.maxEntries >= 2 ? store.maxEntries : 10_000;
  const highWater = Math.max(2, Math.floor(maxEntries * 0.8));
  const target = Math.max(1, Math.floor(maxEntries * 0.5));
  let total = store.entries().length;
  const compacting = total > highWater;
  const sorted = [...candidates].sort((a, b) => (a.completedAt ?? a.order ?? 0) - (b.completedAt ?? b.order ?? 0));
  let removed = 0;
  for (const item of sorted) {
    const expired = Number.isFinite(item.completedAt) && now - item.completedAt >= retentionMs;
    if (!expired && (!compacting || total <= target)) break;
    let deleted = false;
    for (const extraKey of item.extraKeys || []) {
      try { if (store.delete(extraKey)) total--; } catch {}
    }
    try { deleted = store.delete(item.key); } catch {}
    if (deleted) { total--; removed++; }
  }
  return removed;
}

export function openTimelineLocalStore(api) {
  return openProjectionLocalStore(api,'timeline-projection-v2');
}

export function openProjectionLocalStore(api,namespace) {
  const resolve=api?.runtime?.state?.resolveStateDir;
  if(typeof resolve!=='function')return null;
  if(!['timeline-projection-v2','telegram-media-v2','telegram-artifact-v2','telegram-native-cards-v1','telegram-reference-replies-v1'].includes(namespace))throw new Error('TIMELINE_STORAGE_CONFIG_INVALID');
  const directory=path.join(resolve(),'agentbridge',namespace);
  if(!opened.has(directory))opened.set(directory,new TimelineLocalStore(directory));
  return opened.get(directory);
}
