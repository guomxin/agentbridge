/** Copy only an already unexpired access token through official SDK read/upsert.
 * No OAuth resolver/refresh is called, so production profiles cannot be rewritten.
 */
import {readFile,realpath,stat} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

export async function transferUnexpired(input) {
  const home=await realpath(input.fixtureHome),source=await realpath(input.sourceAgentDir),target=await realpath(input.targetAgentDir);
  const marker=JSON.parse(await readFile(path.join(home,'skill-host-acceptance.json'),'utf8'));
  if(marker.fixture!=='skill-host-acceptance-v1'||marker.consumer!=='single-query-real-host-v1'||(await stat(home)).mode&0o077)
    throw new Error('Invalid isolated fixture');
  if(target!==path.join(home,'openclaw','agents','main','agent')||source===target||source.startsWith(home+path.sep))
    throw new Error('Invalid isolated destination');
  const info=await stat(source);
  if(!info.isDirectory()||info.uid!==process.getuid())throw new Error('Unexpected model source ownership');
  const config=JSON.parse(await readFile(input.sourceConfig,'utf8'));
  const profiles=Object.entries(config.auth?.profiles??{}).filter(([,value])=>value.provider==='openai'&&value.mode==='oauth');
  if(profiles.length!==1)throw new Error('Expected one official subscription profile');
  const sdk=await import(pathToFileURL(input.sdkPath));
  const persisted=sdk.findPersistedAuthProfileCredential({agentDir:source,profileId:profiles[0][0]});
  if(persisted?.provider!=='openai'||persisted.type!=='oauth'||typeof persisted.access!=='string'||persisted.access.length<20
      ||!Number.isFinite(persisted.expires)||persisted.expires<=Date.now()+300_000)
    throw new Error('Unexpired model access unavailable; no refresh attempted');
  sdk.upsertAuthProfile({profileId:'openai:skill-host-acceptance',credential:{type:'token',provider:'openai',
    token:persisted.access,expires:persisted.expires,copyToAgents:false},agentDir:target});
  return {status:'prepared',provider:'openai',type:'token',expires:persisted.expires,sourceModified:false,refreshTokenCopied:false};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
 try{const value=await transferUnexpired(JSON.parse(await readFile(process.argv[2],'utf8')));process.stdout.write(JSON.stringify(value)+'\n');}
 catch{process.stderr.write('Unexpired official model access unavailable; source profiles untouched and credentials withheld.\n');process.exitCode=1;}
}
