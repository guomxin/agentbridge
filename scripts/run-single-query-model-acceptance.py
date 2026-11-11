#!/usr/bin/env python3
"""Explicit opt-in real model acceptance, with isolated history source readers."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.support.single_query_model_acceptance import SingleQueryModelAcceptance


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path,required=True)
    parser.add_argument('--node',type=Path,required=True)
    parser.add_argument('--openclaw',type=Path,required=True)
    parser.add_argument('--provider-plugin',type=Path,required=True)
    parser.add_argument('--source-config',type=Path)
    parser.add_argument('--source-agent-dir',type=Path)
    parser.add_argument('--case',choices=['latest','paraphrase','unknown'],default='latest')
    parser.add_argument('--model',default='openai/gpt-5.6-sol')
    parser.add_argument('--timeout',type=int,default=180)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare',action='store_true')
    mode.add_argument('--run',action='store_true')
    args=parser.parse_args()
    if args.run and (not args.source_config or not args.source_agent_dir):parser.error('Explicit official model source paths required')
    if not 1<=args.timeout<=180:parser.error('Budget must be 1..180 seconds')
    fixture=SingleQueryModelAcceptance(args.home,case=args.case)
    try:
        fixture.configure(provider_plugin=args.provider_plugin,model=args.model,mcp_url='http://127.0.0.1:1/mcp' if args.prepare else None)
        fixture.validate_host(node=args.node,openclaw=args.openclaw)
        if args.run:
            fixture.prepare_auth(node=args.node,sdk=args.openclaw.parent/'dist/plugin-sdk/agent-runtime.js',
                source_config=args.source_config,source_agent_dir=args.source_agent_dir)
            fixture.prepare_provider(node=args.node,openclaw=args.openclaw,provider_plugin=args.provider_plugin)
            fixture.start_gateway(node=args.node,openclaw=args.openclaw)
            fixture.run(node=args.node,openclaw=args.openclaw,timeout=args.timeout)
        else:fixture.report.update(model_attempted=False,automatic_transport_status='not_run')
    finally:
        fixture.save_report();fixture.close()
    print(json.dumps({'home':str(fixture.home),'report':str(fixture.home/'evidence/report.json'),
        'model_attempted':fixture.report['model_attempted'],'status':fixture.report.get('automatic_transport_status'),
        'elapsed_seconds':fixture.report.get('elapsed_seconds'),'answer':fixture.report.get('answer_file')},ensure_ascii=False))


if __name__=='__main__':main()
