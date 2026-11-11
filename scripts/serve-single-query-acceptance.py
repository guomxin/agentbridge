#!/usr/bin/env python3
"""Serve isolated single-query projections, with real HTTP/stores and synthetic source I/O.

This server never dispatches a model or invokes real OA. Use a new private home.
The generated login.json is synthetic. Local fault markers never reach production.
"""
from pathlib import Path
import argparse
import json
import math
import os
import signal
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.support.single_query_acceptance import SingleQueryAcceptanceFixture


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',type=Path)
    parser.add_argument('--shutdown-after',type=float,default=1800)
    args=parser.parse_args()
    if not math.isfinite(args.shutdown_after) or args.shutdown_after<0:parser.error('Invalid duration')
    os.umask(0o077)
    home=args.home or Path(tempfile.mkdtemp(prefix='single-query-acceptance-'))
    fixture=SingleQueryAcceptanceFixture(home)
    try:
        url=fixture.start()
        for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_args:fixture.stop.set())
        print(json.dumps({'workspaceUrl':url,'home':str(fixture.home),'loginFile':str(fixture.home/'login.json')},ensure_ascii=False),flush=True)
        deadline=time.monotonic()+args.shutdown_after if args.shutdown_after else None
        while not fixture.stop.wait(.1):
            fixture.controls()
            if deadline is not None and time.monotonic()>=deadline:break
    finally:fixture.close()


if __name__=='__main__':main()
