"""Temporary launch compatibility; all behavior belongs to agentbridge."""
import sys


def main(argv=None):
    from agentbridge.cli.main import main as run
    print("bscli is deprecated and will be removed in AgentBridge 0.3.0; "
          "use agentbridge or python -m agentbridge.", file=sys.stderr)
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
