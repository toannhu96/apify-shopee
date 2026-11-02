"""Chrome session handoff through SSH or a local Docker container, never disk."""
import argparse
import json
import re
import shlex
import subprocess
import time

from shopee_contracts import product_id
from shopee_service.browser_capture import capture_browser, validate_cdp_url

ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
SSH_TARGET = re.compile(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*\Z')
CONTAINER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z')
REFRESH_STATUSES = {'authentication', 'verification', 'pending', 'signing'}


class RefreshError(ValueError):
    """Safe CLI failure without remote stderr or credential-bearing payloads."""


class Remote:
    def __init__(self, target, *, container=None, runner=subprocess.run):
        if not SSH_TARGET.fullmatch(target):
            raise ValueError('Invalid SSH target; use a configured SSH host or user@host')
        if container is not None and not CONTAINER.fullmatch(container):
            raise ValueError('Invalid container name')
        self.target, self.container, self.runner = target, container, runner

    def _argv(self, arguments):
        command = ['python', '-m', 'shopee_service.cli', *arguments]
        if self.container:
            command = ['docker', 'exec', '-i', self.container, *command]
        return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', self.target, shlex.join(command)]

    def call(self, arguments, payload=None):
        argv = self._argv(arguments)
        encoded = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
        if encoded is not None and len(encoded) > 1_179_648:
            raise RefreshError('Captured session exceeds transfer limit')
        try:
            result = self.runner(argv, input=encoded, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=150, check=False)
            if result.returncode or len(result.stdout) > 65_536:
                raise RefreshError('Private service command failed; check service status and account version')
            return json.loads(result.stdout)
        except RefreshError:
            raise
        except Exception:  # noqa: BLE001 - subprocess errors may retain credential input.
            raise RefreshError('Private service command or response failed') from None

    def account(self, alias):
        rows = self.call(['list-accounts'])
        if not isinstance(rows, list):
            raise RefreshError('Unexpected account status response')
        matches = [row for row in rows if isinstance(row, dict) and row.get('alias') == alias]
        if len(matches) != 1 or type(matches[0].get('version')) is not int or matches[0]['version'] < 1:
            raise RefreshError('Account not found or version is invalid')
        return matches[0]


class Local(Remote):
    """Use the same private stdin protocol without requiring a local SSH server."""

    def __init__(self, container, *, runner=subprocess.run):
        if not CONTAINER.fullmatch(container):
            raise ValueError('Invalid local container name')
        self.container, self.runner = container, runner

    def _argv(self, arguments):
        return ['docker', 'exec', '-i', self.container, 'python', '-m', 'shopee_service.cli', *arguments]


def refresh_once(remote, alias, item_id, cdp_url, *, capture=capture_browser):
    if not ALIAS.fullmatch(alias):
        raise RefreshError('Invalid account alias')
    item_id = product_id(item_id)
    validate_cdp_url(cdp_url)
    current = remote.account(alias)  # Fence before touching the browser.
    if current.get('status') == 'disabled':
        raise RefreshError('Account is disabled')
    captured = capture(cdp_url, item_id)
    result = remote.call(['refresh-session-stdin', alias, '--expected-version', str(current['version']),
                          '--item-id', item_id],
                         {'cookie_source': captured.cookie_source, 'headers': captured.headers})
    if (not isinstance(result, dict) or result.get('alias') != alias
            or type(result.get('version')) is not int or result['version'] != current['version'] + 1
            or result.get('verified') is not True or result.get('status') != 'healthy'):
        raise RefreshError('Session replacement did not complete healthy verification; refresh stopped')
    return {'alias': alias, 'version': result['version'], 'status': 'healthy', 'verified': True}


def run_watch(remote, alias, item_id, cdp_url, *, poll_seconds=30, cooldown_seconds=300,
              sleep=time.sleep, clock=time.monotonic, refresh=refresh_once, report=print):
    if poll_seconds < 30 or cooldown_seconds < 300:
        raise RefreshError('Watch requires poll >=30 seconds and cooldown >=300 seconds')
    last_refresh = float('-inf')
    awaiting_healthy = False
    while True:
        account = remote.account(alias)
        status = account.get('status')
        if status == 'disabled':
            raise RefreshError('Account is disabled; watch stopped')
        if status == 'healthy':
            awaiting_healthy = False
        elif status in REFRESH_STATUSES:
            if awaiting_healthy:
                raise RefreshError('Session failed again before a healthy poll; watch stopped')
            if clock() - last_refresh >= cooldown_seconds:
                result = refresh(remote, alias, item_id, cdp_url)
                report(json.dumps(result))
                last_refresh = clock()
                awaiting_healthy = True
        sleep(poll_seconds)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('alias')
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--ssh-target')
    destination.add_argument('--local-container', help='Use an existing local service container without SSH')
    parser.add_argument('--container')
    parser.add_argument('--cdp-url', required=True)
    parser.add_argument('--item-id', required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--once', action='store_true', help='Refresh once (default)')
    modes.add_argument('--watch', action='store_true')
    parser.add_argument('--poll-seconds', type=int, default=30)
    parser.add_argument('--cooldown-seconds', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if not ALIAS.fullmatch(args.alias):
            raise RefreshError('Invalid account alias')
        item = product_id(args.item_id)
        validate_cdp_url(args.cdp_url)
        if args.local_container and args.container:
            raise RefreshError('Use --container only with --ssh-target')
        remote = (Local(args.local_container) if args.local_container
                  else Remote(args.ssh_target, container=args.container))
        if args.watch:
            run_watch(remote, args.alias, item, args.cdp_url, poll_seconds=args.poll_seconds,
                      cooldown_seconds=args.cooldown_seconds)
        else:
            print(json.dumps(refresh_once(remote, args.alias, item, args.cdp_url)))
        return 0
    except (ValueError, KeyboardInterrupt) as error:
        print(str(error) if isinstance(error, ValueError) else 'Refresh stopped')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
