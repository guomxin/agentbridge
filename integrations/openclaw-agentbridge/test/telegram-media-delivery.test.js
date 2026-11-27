import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {checkedWorkspaceMedia,materializeWorkspaceMedia,MAX_TELEGRAM_MEDIA_BYTES} from '../lib/telegram-media-delivery.js';

const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=','base64');
const source=()=>({fileName:'测试图片.png',mimeType:'image/png',byteSize:png.length,sha256:createHash('sha256').update(png).digest('hex'),
  contentBase64:png.toString('base64'),expiresAt:new Date(Date.now()+60_000).toISOString()});

test('a bounded original image keeps exact bytes and requires top-level forceDocument',async()=>{
 const calls=[];const result=await materializeWorkspaceMedia(source(),{ordinal:1,partCount:3,
  saveMediaBuffer:async(...args)=>{calls.push(args);return {path:'/private/synthetic/image.png',size:png.length};}});
 assert.equal(calls.length,1);assert.deepEqual(calls[0][0],png);assert.equal(calls[0][3],MAX_TELEGRAM_MEDIA_BYTES);
 assert.equal(result.forceDocument,true);assert.equal(result.payload.forceDocument,undefined);
 assert.equal(result.payload.text,'【网页附件 2/3】');assert.equal(result.byteSize,png.length);
 assert.deepEqual(await result.mediaReadFile('/private/synthetic/image.png'),png);
 await assert.rejects(result.mediaReadFile('/private/synthetic/other.png'),/MEDIA_LOCAL_PATH_MISMATCH/);
});

test('changed bytes, mismatched type, expired source, unsupported document and malformed base64 never materialize',async()=>{
 for(const change of [{sha256:'a'.repeat(64)},{byteSize:png.length+1},{mimeType:'image/jpeg'},
  {mimeType:'text/csv'},{mimeType:'application/pdf'},{expiresAt:new Date(0).toISOString()},
  {contentBase64:source().contentBase64+'\n'},{byteSize:MAX_TELEGRAM_MEDIA_BYTES+1},{contentBase64:'https://example.test/file'}]){
  let writes=0;await assert.rejects(materializeWorkspaceMedia({...source(),...change},{ordinal:0,partCount:1,
   saveMediaBuffer:async()=>{writes++;return {path:'/tmp/never',size:1};}}),/MEDIA_SOURCE_/);assert.equal(writes,0);
 }
});

test('partial groups use their original ordinal and never materialize a whole group or invent a URL fallback',async()=>{
 let writes=0;await assert.rejects(materializeWorkspaceMedia(source(),{ordinal:2,partCount:2,
  saveMediaBuffer:async()=>{writes++;}}),/MEDIA_PART_INVALID/);assert.equal(writes,0);
 assert.deepEqual(checkedWorkspaceMedia(source()).body,png);
 await assert.rejects(materializeWorkspaceMedia(source(),{ordinal:0,partCount:1,
  saveMediaBuffer:async()=>{throw new Error('disk full');}}),/disk full/);
});

test('the full six MiB limit and Unicode filenames do not overflow validation or use UTF-16 length as a character limit',()=>{
 const body=Buffer.alloc(MAX_TELEGRAM_MEDIA_BYTES);Buffer.from('89504e470d0a1a0a','hex').copy(body);
 const large={...source(),fileName:'😀'.repeat(120)+'.png',byteSize:body.length,
  sha256:createHash('sha256').update(body).digest('hex'),contentBase64:body.toString('base64')};
 assert.equal(checkedWorkspaceMedia(large).byteSize,body.length);
 assert.throws(()=>checkedWorkspaceMedia({...large,contentBase64:large.contentBase64.slice(0,-1)+'='}),/MEDIA_SOURCE_INTEGRITY_FAILED/);
});
