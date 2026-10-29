/** Minimal official-profile transfer for isolated host acceptance.
 * No production sessions, refresh tokens, provider config or profile identifiers
 * are copied. Only an unexpired access token is written through the SDK API.
 */
import {readFile, realpath, stat} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

export function shortLivedCredential(resolved, persisted, now = Date.now()) {
  const value = resolved?.credential ?? persisted;
  if (value?.provider !== 'openai' || !['oauth', 'token'].includes(value.type)) {
    throw new Error('Only the explicitly selected OpenAI subscription profile is supported');
  }
  const token = resolved?.apiKey;
  const expires = Number(value.expires);
  if (typeof token !== 'string' || token.length < 20 || !Number.isFinite(expires) || expires <= now + 60_000) {
    throw new Error('The official resolver did not return a usable short-lived access token');
  }
  return {type: 'token', provider: 'openai', token, expires, copyToAgents: false};
}

export async function transfer({sdkPath, sourceConfig, sourceAgentDir, targetAgentDir, fixtureHome}) {
  const home = await realpath(fixtureHome);
  const marker = JSON.parse(await readFile(path.join(home, 'skill-host-acceptance.json'), 'utf8'));
  if (marker.fixture !== 'skill-host-acceptance-v1') throw new Error('Missing isolated fixture marker');
  const source = await realpath(sourceAgentDir);
  const target = await realpath(targetAgentDir);
  if (target !== path.join(home, 'openclaw', 'agents', 'main', 'agent') || source === target || source.startsWith(home + path.sep)) {
    throw new Error('Invalid isolated authentication destination');
  }
  if ((await stat(home)).mode & 0o077) throw new Error('The isolated home must be private');
  const cfg = JSON.parse(await readFile(sourceConfig, 'utf8'));
  const profiles = Object.entries(cfg.auth?.profiles ?? {}).filter(([, value]) => value.provider === 'openai' && value.mode === 'oauth');
  if (profiles.length !== 1) throw new Error('Expected exactly one configured OpenAI subscription profile');
  const [profileId] = profiles[0];
  const sdk = await import(pathToFileURL(sdkPath));
  const persisted = sdk.findPersistedAuthProfileCredential({agentDir: source, profileId});
  if (!persisted) throw new Error('The selected persisted authentication profile is missing');
  const store = sdk.loadAuthProfileStoreWithoutExternalProfiles(source, {allowKeychainPrompt: false});
  const resolved = await sdk.resolveApiKeyForProfile({cfg, store, profileId, agentDir: source});
  // Re-read after the official resolver may have refreshed the original profile.
  const fresh = sdk.findPersistedAuthProfileCredential({agentDir: source, profileId});
  const credential = shortLivedCredential(resolved, fresh);
  sdk.upsertAuthProfile({profileId: 'openai:skill-host-acceptance', credential, agentDir: target});
  return {status: 'prepared', provider: 'openai', type: 'token', expires: credential.expires, refreshTokenCopied: false};
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const input = JSON.parse(await readFile(process.argv[2], 'utf8'));
    process.stdout.write(JSON.stringify(await transfer(input)) + '\n');
  } catch {
    // Provider errors can contain credentials. Keep the process boundary quiet.
    process.stderr.write('Official model authentication preparation failed; no credentials were printed.\n');
    process.exitCode = 1;
  }
}
