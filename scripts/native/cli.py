"""Native macOS maintenance entry. Planning is the release default."""
from __future__ import annotations
import argparse
import json
import sys
import uuid
from pathlib import Path
from common import external, save, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', help='Private JSON result outside the repository')
    commands = parser.add_subparsers(dest='command', required=True)
    validation = commands.add_parser('validate')
    validation.add_argument('--full', action='store_true')
    validation.add_argument('--plugin', action='store_true')
    validation.add_argument('--test', action='append', default=[])
    publish = commands.add_parser('publish')
    publish.add_argument('--profile', required=True)
    publish.add_argument('--host', default='10.10.50.213')
    publish.add_argument('--user', default='root')
    publish.add_argument('--remote-root', default='/home/guomao/agentbridge')
    publish.add_argument('--identity', required=True)
    publish.add_argument('--known-hosts', required=True)
    publish.add_argument('--github-identity')
    publish.add_argument('--github-known-hosts')
    publish.add_argument('--host-profile')
    publish.add_argument('--authorization')
    publish.add_argument('--identity-label', action='append', default=[])
    publish.add_argument('--expect-endpoint', action='append', default=[])
    publish.add_argument('--offline', action='store_true', help='No network, predecessor remains unverified')
    publish.add_argument('--apply', action='store_true', help='Explicit production operation, requires reviewed host authorization')
    publish.add_argument('--resume', action='store_true')
    publish.add_argument('--reuse-validation', action='store_true')
    host = commands.add_parser('host')
    host.add_argument('action', choices=['render', 'review', 'register', 'status', 'start', 'stop', 'restart'])
    host.add_argument('--profile', required=True)
    host.add_argument('--destination')
    host.add_argument('--ownership-evidence')
    host.add_argument('--kind', choices=['gateway', 'tunnel'], default='gateway')
    host.add_argument('--authorization')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12+ required')
    report = external(args.report) if args.report else Path.home() / '.local/state/agentbridge/maintenance' / ('native-run-' + uuid.uuid4().hex + '.json')
    try:
        if args.command == 'validate':
            from validation import validate
            result = validate(full=args.full, tests=args.test, plugin=args.plugin)
        elif args.command == 'publish':
            if args.apply and (not args.host_profile or not args.authorization):
                raise ValueError('--apply requires --host-profile and --authorization')
            from publish import Publisher
            publisher = Publisher(args)
            result = publisher.execute() if args.apply else publisher.plan(offline=args.offline)
        else:
            import lifecycle
            config = lifecycle.settings(args.profile)
            if args.action == 'render':
                if not args.destination:
                    raise ValueError('render requires a new --destination')
                result = lifecycle.render(config, args.destination)
            elif args.action == 'review':
                result = lifecycle.review(config)
            elif args.action == 'register':
                if not args.destination or not args.ownership_evidence:
                    raise ValueError('Registration requires --destination and --ownership-evidence')
                result = lifecycle.register(config, args.ownership_evidence, args.destination)
            else:
                if args.action in ('start', 'restart') and not args.authorization:
                    raise ValueError('Activation requires --authorization')
                result = lifecycle.manage(config, args.action, args.kind, args.authorization)
        result.update(checkedAt=timestamp(), exitCode=0)
        code = 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        # No child stdout/stderr or private configuration in this report.
        result = {'status': 'failed', 'errorType': type(error).__name__,
                  'checkedAt': timestamp(), 'exitCode': 2}
        if type(error) in (ValueError, RuntimeError):
            result['reason'] = str(error)
        print('Native maintenance failed: ' + type(error).__name__ + '; inspect inputs and private evidence.', file=sys.stderr)
        code = 2
    if report:
        result['command'] = args.command
        save(report, result)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
