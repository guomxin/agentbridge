"""Stateless skill authoring. The provider receives two messages and no tools."""

import asyncio
import json
import sys
from pathlib import Path

from .private_files import private_text


def validate_inputs(system, prompt):
    if (
        not isinstance(system, str)
        or not isinstance(prompt, str)
        or not prompt.strip()
        or len(system) > 56_000
        or len(prompt) > 24_000
    ):
        raise ValueError("Invalid tool-free skill model request")


async def provider_completion(config, system, prompt):
    from nanobot.providers.factory import make_provider

    validate_inputs(system, prompt)
    config.agents.defaults.fallback_models = []
    model = config.resolve_preset().model
    provider = make_provider(config)
    result = await provider.chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        model=model,
        tools=None,
        max_tokens=6000,
    )
    if (
        result.finish_reason != "stop"
        or result.tool_calls
        or not isinstance(result.content, str)
        or not result.content.strip()
        or len(result.content) > 48_000
    ):
        raise RuntimeError("Incomplete tool-free model response")
    usage = result.usage
    return {
        "text": result.content,
        "model": model,
        "execution": "tool_free_completion",
        "usage": (
            {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "totalTokens": usage.total_tokens,
            }
            if usage
            else None
        ),
    }


async def run_worker(request):
    from nanobot.config.loader import set_config_path
    from nanobot.config.schema import Config

    from .worker import authenticated_session

    async with authenticated_session(request) as (_, _, level):
        if level != "L3":
            raise PermissionError("Skill model requires the approved host")
        config_path = Path(request["userDir"]) / "config.json"
        set_config_path(config_path)
        config = Config.model_validate(
            json.loads(private_text(Path(request["modelConfigFile"])))
        )
        config.bind_source_path(config_path)
        return await provider_completion(config, request["system"], request["prompt"])


async def complete_for_binding(host, record, system, prompt):
    validate_inputs(system, prompt)
    request = host.worker_payload(record, None)
    request.update(system=system, prompt=prompt)
    home = Path(request["userDir"])
    bootstrap = (
        "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
        "runpy.run_module('nanobot_agentbridge.skill_model',run_name='__main__')"
    )
    async with asyncio.timeout(95), host.subject_locks[record["user_subject"]]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            "-c",
            bootstrap,
            str(Path(__file__).resolve().parents[1]),
            env=host.worker_environment(home),
            cwd=home,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await process.communicate(json.dumps(request).encode())
            if process.returncode or len(output) > 512_000:
                raise RuntimeError("Tool-free model unavailable")
            return json.loads(output)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()


if __name__ == "__main__":
    try:
        result = asyncio.run(run_worker(json.load(sys.stdin)))
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        raise SystemExit(1) from None
