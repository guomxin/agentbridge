"""Isolated loopback capacity experiment. Never accepts a production target."""
from __future__ import annotations

from contextlib import closing
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
def peak_rss_mib():
    import resource
    scale = 1024**2 if sys.platform == 'darwin' else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from pydantic import AnyHttpUrl

from agentbridge.core.mcp_identities import McpIdentityTokenStore
from agentbridge.mcp.central import StoredIdentityTokenVerifier
from agentbridge.mcp.resource_budget import BoundedMcpProtocol, bounded_mcp_app


async def measure(rounds, levels):
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('mcp').setLevel(logging.WARNING)
    with tempfile.TemporaryDirectory(prefix='agentbridge-capacity-') as directory:
        root = Path(directory)
        store = McpIdentityTokenStore(root / 'identities.sqlite3')
        tokens = [store.issue(user_subject=f'capacity-{i}', expected_principal_ref=f'Synthetic {i}')['token']
                  for i in range(16)]
        database = root / 'sample.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('CREATE TABLE samples (value INTEGER)')
            connection.executemany('INSERT INTO samples VALUES (?)', [(i,) for i in range(100)])
            connection.commit()
        mcp = FastMCP('isolated-capacity', stateless_http=True, json_response=True,
            token_verifier=StoredIdentityTokenVerifier(store, resource='http://localhost/mcp'),
            auth=AuthSettings(issuer_url=AnyHttpUrl('http://localhost'), resource_server_url=AnyHttpUrl('http://localhost/mcp'), required_scopes=[]))

        @mcp.tool(structured_output=True)
        async def sample_read() -> dict[str, int]:
            # A fixed synthetic downstream wait, NOT a claim about OA/model latency.
            await asyncio.sleep(.05)
            with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)) as connection:
                count, total = connection.execute('SELECT count(*), sum(value) FROM samples').fetchone()
            return {'count': count, 'total': total}

        config = uvicorn.Config(bounded_mcp_app(mcp, store), host='127.0.0.1', port=0,
            http=BoundedMcpProtocol, ws='none', log_level='critical',
            limit_concurrency=128, backlog=64, timeout_keep_alive=5)
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(1000):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(.01)
            if not server.started:
                raise RuntimeError('isolated MCP startup timeout')
            port = server.servers[0].sockets[0].getsockname()[1]
            rows = []
            diagnostics = []
            for concurrency in levels:
                # Close each level's pool so idle connections cannot bias the next level.
                async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=64)) as client:
                    async def request(index):
                        started = time.perf_counter()
                        try:
                            response = await client.post(f'http://127.0.0.1:{port}/mcp',
                                headers={'Authorization': f'Bearer {tokens[index % len(tokens)]}',
                                         'Accept': 'application/json, text/event-stream'},
                                json={'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                                      'params': {'name': 'sample_read', 'arguments': {}}})
                            valid = (response.status_code == 200 and
                                response.json().get('result', {}).get('structuredContent') == {'count': 100, 'total': 4950})
                            if not valid and len(diagnostics) < 1:
                                diagnostics.append(response.text[:1200])
                            status = str(response.status_code) if valid or response.status_code != 200 else 'invalid_result'
                        except Exception as error:
                            status = type(error).__name__
                        return status, (time.perf_counter() - started) * 1000
                    started, cpu = time.perf_counter(), time.process_time()
                    results = []
                    for _ in range(rounds):
                        results.extend(await asyncio.gather(*(request(i) for i in range(concurrency))))
                    elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
                    latencies = sorted(latency for _, latency in results)
                    statuses = {status: sum(s == status for s, _ in results) for status, _ in results}
                    rows.append({'concurrency': concurrency, 'requests': len(results), 'statuses': statuses,
                        'elapsed_seconds': round(elapsed, 3), 'requests_per_second': round(len(results) / elapsed, 2),
                        'p50_ms': round(statistics.median(latencies), 2),
                        'p95_ms': round(latencies[max(0, math.ceil(len(latencies) * .95) - 1)], 2),
                        'cpu_seconds': round(cpu_seconds, 3),
                        'process_peak_rss_mib': peak_rss_mib()})
                await asyncio.sleep(.1)

            source_root = Path(__file__).resolve().parents[1]
            return {'schema': 'agentbridge.capacity.v1', 'measured_at': datetime.now(timezone.utc).isoformat(),
                'source_sha256': {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                    for name in ('agentbridge/mcp/resource_budget.py', 'agentbridge/mcp/central.py', 'scripts/measure_mcp_capacity.py')},
                'environment': {
                'platform': platform.platform(), 'python': platform.python_version(), 'logical_cpus': os.cpu_count(),
                'uvicorn': uvicorn.__version__}, 'scope': 'isolated loopback MCP/auth/SQLite; 16 synthetic subjects; 50ms fake downstream; client and server share process; RSS includes client and server; no TLS or production/model load',
                'rounds': rounds, 'diagnostics': diagnostics, 'results': rows}
        finally:
            server.should_exit = True
            await task


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--levels', type=int, nargs='+', default=[1, 8, 16, 32, 64])
    args = parser.parse_args()
    if any(level < 1 or level > 64 for level in args.levels):
        parser.error('levels must be between 1 and 64')
    if not 1 <= args.rounds <= 100:
        parser.error('rounds must be between 1 and 100')
    report = asyncio.run(measure(args.rounds, args.levels))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if all(set(row['statuses']) == {'200'} for row in report['results']) else 1)
