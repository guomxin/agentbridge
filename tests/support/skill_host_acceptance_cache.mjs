/** Copy only exact public Codex runtime tarballs into an isolated npm cache.
 * npm still validates the registry lockfile integrity during official install.
 * No npm configuration, credentials, arbitrary cached URLs or sessions migrate.
 */
import {readFile, realpath, stat} from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';
import {pipeline} from 'node:stream/promises';
import {pathToFileURL} from 'node:url';

export function publicRuntimeMetadata(metadata, url) {
  if (metadata?.url !== url) throw new Error('Unexpected public cache URL');
  const names = ['cache-control', 'content-type', 'date', 'etag', 'last-modified'];
  return {time: metadata.time, url, reqHeaders: {}, resHeaders: Object.fromEntries(
    names.filter(name => typeof metadata.resHeaders?.[name] === 'string').map(name => [name, metadata.resHeaders[name]])), options: {}};
}

export async function copyRuntimeCache({npmPackage, sourceCache, fixtureHome, version}) {
  if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('Expected an exact public Codex version');
  const home = await realpath(fixtureHome);
  const marker = JSON.parse(await readFile(path.join(home, 'skill-host-acceptance.json'), 'utf8'));
  if (marker.fixture !== 'skill-host-acceptance-v1' || (await stat(home)).mode & 0o077) throw new Error('Invalid isolated fixture');
  const cache = createRequire(npmPackage)('cacache');
  const target = path.join(home, 'npm-cache', '_cacache');
  const copied = [], missing = [];
  for (const platform of ['darwin-arm64', 'darwin-x64', 'linux-arm64', 'linux-x64', 'win32-arm64', 'win32-x64']) {
    const url = `https://registry.npmjs.org/@openai/codex/-/codex-${version}-${platform}.tgz`;
    const key = 'make-fetch-happen:request-cache:' + url;
    const info = await cache.get.info(sourceCache, key);
    if (!info) { missing.push(platform); continue; }
    await pipeline(cache.get.stream(sourceCache, key), cache.put.stream(target, key,
      {integrity: info.integrity, metadata: publicRuntimeMetadata(info.metadata, url)}));
    copied.push(platform);
  }
  return {copied, missing};
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const args = JSON.parse(await readFile(process.argv[2], 'utf8'));
    process.stdout.write(JSON.stringify(await copyRuntimeCache(args)) + '\n');
  } catch {
    process.stderr.write('Public runtime cache transfer failed; no cache content was printed.\n');
    process.exitCode = 1;
  }
}
