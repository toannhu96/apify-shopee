"""Server-only account administration. Credential contents are never printed."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from urllib.parse import urlsplit

from .config import Settings
from .secrets import Cipher


class OperatorError(ValueError):
    """Safe message for an operator, with no supplied credential contents."""


def _alias(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise argparse.ArgumentTypeError('Use a 1–64 character account alias with letters, numbers, _ or -')
    return value


def _item(value: str) -> str:
    if not re.fullmatch(r'[1-9][0-9]{0,19}', value):
        raise argparse.ArgumentTypeError('Use a positive decimal product ID of at most 20 digits')
    return value


def _read_private(path: str, maximum: int) -> str:
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise OperatorError('Credential files must be regular files accessible only to their owner (mode 600)')
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise OperatorError('Credential file exceeds the size limit')
        return raw.decode('utf-8')
    except (OSError, UnicodeError):
        raise OperatorError('Credential file could not be read securely') from None


def _seed(path: str) -> dict:
    from shopee_affiliate import signed_client
    try:
        value = json.loads(_read_private(path, signed_client.MAX_STATE_BYTES),
                           object_pairs_hook=signed_client._pairs,
                           parse_constant=signed_client._constant,
                           parse_float=signed_client._finite_float)
        if not isinstance(value, dict) or set(value) != {'headers'}:
            raise ValueError()
        return signed_client._normalized_headers(value['headers'])
    except (ValueError, TypeError, RecursionError):
        raise OperatorError('Signing seed is invalid') from None


def _initialize(headers: dict) -> dict:
    from shopee_affiliate.session_signer import initialize_session, record_response
    # The supplied seed is from an accepted browser request whose response completed.
    return record_response(initialize_session(headers))


def _proxy(path: str | None) -> str | None:
    if path is None:
        return None
    value = _read_private(path, 8192).rstrip('\r\n')
    try:
        parsed = urlsplit(value)
        if (not value.isascii() or any(c.isspace() for c in value)
                or parsed.scheme not in {'http', 'https', 'socks5', 'socks5h'}
                or not parsed.hostname or parsed.port == 0 or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError()
    except ValueError:
        raise OperatorError('Proxy configuration is invalid') from None
    return value


def _credentials(args) -> dict:
    from shopee_affiliate import direct, signed_client
    cookie_source = _read_private(args.cookies_file, signed_client.MAX_COOKIE_INPUT_BYTES)
    try:
        signed_client._cookie_header(cookie_source)
    except direct.DirectError:
        raise OperatorError('Cookie export is invalid or contains no usable session cookies') from None
    return {'cookie_source': cookie_source, 'signing_state': _initialize(_seed(args.state_file)),
            'proxy_url': _proxy(args.proxy_file)}


def _repository():
    from .repository import Repository
    settings = Settings.from_env()
    return Repository(settings.database_url, Cipher(settings.secret_key),
                      min_interval_seconds=settings.min_interval_seconds,
                      lease_seconds=settings.lease_seconds)


def _verify(repo, alias: str, item_id: str) -> bool:
    from .worker import verify_account
    return verify_account(repo, alias, item_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init-db', help='Apply backend database migrations')
    commands.add_parser('list-accounts', help='Show safe account aliases and health only')
    rotate = commands.add_parser('rotate-secret-key', help='Re-encrypt stored sessions during stopped-service maintenance')
    rotate.add_argument('--new-key-file', required=True, help='Protected new Fernet key file (mode 600)')
    rotate.add_argument('--maintenance-confirmed', action='store_true',
                        help='Confirm all API, worker and account-administration processes are stopped')
    for command in ('add-account', 'replace-session'):
        sub = commands.add_parser(command, help='Install a pending session; verify it before use')
        sub.add_argument('alias', type=_alias)
        sub.add_argument('--cookies-file', required=True, help='Protected cookie file (mode 600)')
        sub.add_argument('--state-file', required=True, help='Protected signing seed JSON (mode 600)')
        sub.add_argument('--proxy-file', help='Optional protected proxy URL file (mode 600)')
    disable = commands.add_parser('disable-account', help='Prevent new requests using this account')
    disable.add_argument('alias', type=_alias)
    verify = commands.add_parser('verify-account', help='Make one live lookup to validate and activate a session')
    verify.add_argument('alias', type=_alias)
    verify.add_argument('--item-id', type=_item, required=True)
    return parser


def _run(args, repo) -> int:
    if args.command == 'init-db':
        repo.migrate()
        print('Database migrations applied.')
    elif args.command == 'rotate-secret-key':
        from .rotate import RotationError, rotate_credentials
        try:
            result = rotate_credentials(repo, _read_private(args.new_key_file, 128).rstrip('\r\n'),
                                        maintenance_confirmed=args.maintenance_confirmed)
        except RotationError as error:
            raise OperatorError(str(error)) from None
        print(json.dumps(result, sort_keys=True))
    elif args.command == 'list-accounts':
        rows = [{key: row.get(key) for key in ('alias', 'version', 'status')}
                for row in repo.list_accounts()]
        print(json.dumps(rows, ensure_ascii=True))
    elif args.command == 'disable-account':
        repo.disable_account(args.alias)
        print(f'Account {args.alias} disabled.')
    elif args.command == 'verify-account':
        if not _verify(repo, args.alias, args.item_id):
            raise OperatorError('Account verification did not succeed; inspect the account health and retry after resolving it')
        print(f'Account {args.alias} verified and activated.')
    else:
        if args.command == 'add-account' and any(row.get('alias') == args.alias for row in repo.list_accounts()):
            raise OperatorError('Account alias already exists; use replace-session')
        version = repo.replace_account(args.alias, _credentials(args),
                                       create_only=args.command == 'add-account',
                                       require_existing=args.command == 'replace-session')
        print(f'Account {args.alias} session version {version} is pending verification.')
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args, _repository())
    except OperatorError as error:
        print(str(error), file=sys.stderr)
    except Exception:  # noqa: BLE001 - top-level operator boundary must not print credential-bearing exceptions
        # Database, transport and signer exceptions may contain confidential values.
        print('Operation failed. Check service configuration and account state; no credentials were printed.',
              file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
