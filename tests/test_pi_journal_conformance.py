"""Exercise a real abrupt process exit after durable Pi request acceptance."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT/"integrations/pi-agentbridge"


def test_interrupted_accepted_run_is_reconciled_without_starting_it_again(tmp_path):
    def run(program):
        return subprocess.run([sys.executable,"-I","-c","import sys;sys.path.insert(0,sys.argv[1])\n"+program,
                               str(ADAPTER),str(tmp_path)], capture_output=True, text=True, timeout=20)
    interrupted = run("""
import asyncio, json, os
from pathlib import Path
from pi_agentbridge.ledger import Ledger
directory = Path(sys.argv[2])
async def main():
    ledger = Ledger(directory, "pi-office")
    async def work(run, history, emit):
        with (directory/"downstream.count").open("a") as stream:
            stream.write("once")
        emit("tool_started","read")
        os._exit(17)
    accepted = ledger.accept("first","endpoint","request","digest","read",work)
    print(accepted["id"],flush=True)
    await asyncio.sleep(20)
asyncio.run(main())
""")
    assert interrupted.returncode == 17
    recovered = run("""
import asyncio,json
from pathlib import Path
from pi_agentbridge.ledger import Ledger
directory=Path(sys.argv[2])
async def main():
    ledger=Ledger(directory,"pi-office")
    async def forbidden(*args):
        (directory/"replayed").touch()
    row=ledger.accept("first","endpoint","request","digest","read",forbidden)
    await asyncio.sleep(.01)
    print(json.dumps([row["id"],row["state"],[e["kind"] for e in ledger.events(row["id"],0)]]))
    await ledger.close()
asyncio.run(main())
""")
    assert recovered.returncode == 0
    assert json.loads(recovered.stdout) == [interrupted.stdout.strip(),"unknown",["accepted","tool_started","unknown"]]
    assert (tmp_path/"downstream.count").read_text() == "once"
    assert not (tmp_path/"replayed").exists()
