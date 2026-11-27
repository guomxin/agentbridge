import {createHash} from 'node:crypto';
import path from 'node:path';

export const MAX_TELEGRAM_MEDIA_BYTES = 6 * 1024 * 1024;
export const workspaceMediaMode = 'workspace_media_v2';
const TYPES = new Map([['image/png', '.png'], ['image/jpeg', '.jpg'], ['image/webp', '.webp']]);

// Only centrally authorized retained bytes enter this path. It never fetches a
// URL or interprets model/caption text as a file reference.
export function checkedWorkspaceMedia(file, now=Date.now()) {
  if (!file || typeof file!=='object' || !TYPES.has(file.mimeType) ||
      !Number.isSafeInteger(file.byteSize) || file.byteSize<1 || file.byteSize>MAX_TELEGRAM_MEDIA_BYTES ||
      !/^[a-f0-9]{64}$/.test(file.sha256 || '') || typeof file.contentBase64!=='string' ||
      file.contentBase64.length!==4*Math.ceil(file.byteSize/3) ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(file.contentBase64) ||
      !(Date.parse(file.expiresAt)>now)) throw new Error('MEDIA_SOURCE_INVALID');
  const body=Buffer.from(file.contentBase64,'base64');
  if (body.length!==file.byteSize || body.toString('base64')!==file.contentBase64 ||
      createHash('sha256').update(body).digest('hex')!==file.sha256) throw new Error('MEDIA_SOURCE_INTEGRITY_FAILED');
  const signature=file.mimeType==='image/png' ? body.subarray(0,8).equals(Buffer.from('89504e470d0a1a0a','hex'))
    : file.mimeType==='image/jpeg' ? body[0]===0xff && body[1]===0xd8 && body[2]===0xff
    : body.subarray(0,4).toString()==='RIFF' && body.subarray(8,12).toString()==='WEBP';
  if (!signature) throw new Error('MEDIA_SOURCE_TYPE_MISMATCH');
  const name=file.fileName;
  if (typeof name!=='string' || !name || !name.isWellFormed() || [...name].length>256 || /[\u0000-\u001f\u007f/\\]/u.test(name) || ['.','..'].includes(name)) throw new Error('MEDIA_SOURCE_FILENAME_INVALID');
  return {body,contentType:file.mimeType,byteSize:file.byteSize,sha256:file.sha256,filename:name};
}

export async function materializeWorkspaceMedia(file,{saveMediaBuffer,ordinal,partCount,now=Date.now()}) {
  if (typeof saveMediaBuffer!=='function' || !Number.isSafeInteger(ordinal) || ordinal<0 || ordinal>3 ||
      !Number.isSafeInteger(partCount) || partCount<1 || partCount>4 || ordinal>=partCount) throw new Error('MEDIA_PART_INVALID');
  const checked=checkedWorkspaceMedia(file,now);
  const saved=await saveMediaBuffer(checked.body,checked.contentType,'inbound',MAX_TELEGRAM_MEDIA_BYTES,
    checked.filename);
  if (typeof saved?.path!=='string' || !path.isAbsolute(saved.path) || saved.size!==checked.byteSize) throw new Error('MEDIA_MATERIALIZATION_FAILED');
  return {payload:{text:`【网页附件 ${ordinal+1}/${partCount}】`,mediaUrl:saved.path},
    // OpenClaw 2026.7.1 reads this from the top-level send context; placing it
    // only inside payload silently permits image conversion via sendPhoto.
    forceDocument:true,localPath:saved.path,sha256:checked.sha256,byteSize:checked.byteSize,
    mediaLocalRoots:[path.dirname(path.resolve(saved.path))],
    mediaReadFile:async requested=>{
      if (typeof requested!=='string' || path.resolve(requested)!==path.resolve(saved.path)) throw new Error('MEDIA_LOCAL_PATH_MISMATCH');
      // Preserve the authorized immutable bytes even if another local process
      // changes the staging file while Central rechecks the send permit.
      return Buffer.from(checked.body);
    }};
}
