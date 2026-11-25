"""Confirm a fresh tool-free Pi model invocation and bind it to a verified release wheel."""
import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import secrets
import sys
import tempfile
from urllib.parse import urlsplit

from common import ROOT, external, private_file, read, run, save
from pi_release import ADAPTER, SDK_VERSION, file_digest, verify_runtime


def fixed_candidate():
    if run(["git", "status", "--porcelain=v1"]):
        raise ValueError("Formal model observation requires a clean fixed candidate")
    commit = run(["git", "rev-parse", "HEAD"])
    artifact = json.loads(run([sys.executable, ROOT / "scripts/agentbridge_artifact.py", "verify", "--root", ROOT]))
    if file_digest(artifact["wheel"]) != artifact["sha256"]:
        raise ValueError("Selected wheel differs from the formal artifact")
    return {"commit": commit, "wheelSha256": artifact["sha256"]}


async def measure(runtime, model, expected_origin):
    url = urlsplit(expected_origin)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or expected_origin != "https://" + url.netloc):
        raise ValueError("Expected the exact approved model service HTTPS origin")
    ready = await runtime.invoke("ready")
    expected_model = model["provider"] + "/" + model["id"]
    if ready.get("serviceOrigin") != expected_origin or ready.get("model") != expected_model:
        raise ValueError("Pi selected a different model service or model")
    challenge = "PI-" + secrets.token_hex(16)
    reply = await runtime.invoke("completion", subject="model-preflight",
        system="这是一次连接验收。仅原样回复用户给出的随机串，不添加标点或解释。", message=challenge)
    if (reply.get("execution") != "tool_free_completion" or reply.get("model") != expected_model
            or reply.get("text", "").strip() != challenge):
        raise ValueError("No verified fresh completion from the selected Pi model")
    return {"execution": "tool_free_completion", "model": expected_model, "modelServiceOrigin": expected_origin,
            "challengeSha256": hashlib.sha256(challenge.encode()).hexdigest(), "toolCount": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-pins", required=True)
    parser.add_argument("--model-config", required=True)
    parser.add_argument("--expected-origin", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = external(args.output)
    if output.exists():
        raise ValueError("Choose a new output file for this live observation")
    candidate = fixed_candidate()
    runtime_spec = read(private_file(args.runtime_pins))
    verify_runtime(runtime_spec)
    config = private_file(args.model_config)
    model_hash = file_digest(config)
    model = read(config)
    if not private_file(model["authFile"]).read_bytes().strip():
        raise ValueError("Expected Pi's independent model authorization")
    sys.path.insert(0, str(ROOT / "integrations/pi-agentbridge"))
    from pi_agentbridge.runtime import PiRuntime
    with tempfile.TemporaryDirectory(prefix="pi-real-model-") as directory:
        runtime = PiRuntime({**runtime_spec, "model": model}, Path(directory))
        result = asyncio.run(measure(runtime, model, args.expected_origin))
    verify_runtime(runtime_spec)
    if candidate != fixed_candidate() or file_digest(config) != model_hash:
        raise ValueError("Candidate or model configuration changed during verification")
    save(output, {"schema": "agentbridge.pi-observation.v1", "case": "real_model", "passed": True,
                  "implementation": "pi", "adapterVersion": ADAPTER, "sdkVersion": SDK_VERSION,
                  "modelConfigSha256": model_hash, "syntheticModel": False, **candidate, **result,
                  "observedAt": datetime.now(UTC).isoformat()})
    print("Verified a fresh tool-free completion from the approved Pi model.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, TimeoutError) as exc:
        raise SystemExit("Pi real-model observation failed (" + type(exc).__name__ + ").") from None
