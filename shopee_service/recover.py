"""Private maintenance: atomically rotate the database password and session key.

Stop API, workers and administration processes first. Configure current
SH_DATABASE_URL/SH_SECRET_KEY and replacement SH_NEXT_DATABASE_PASSWORD/
SH_NEXT_SECRET_KEY privately. After success, update all service configurations
before restoring normal startup. This does not rotate the API bearer token.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from .repository import LOCK_ID
from .rotate import rotate_credentials
from .secrets import Cipher


class RecoveryError(ValueError):
    """Credential-free failure boundary."""


class _TransactionRepository:
    def __init__(self, connection, cipher):
        self.connection, self.cipher = connection, cipher

    @contextmanager
    def _tx(self):
        # Reuse the outer transaction so rekey and ALTER ROLE commit together.
        yield self.connection


def _connect(database_url, password):
    try:
        return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=10)
    except psycopg.OperationalError:
        # A previous commit may have changed authentication before config was
        # updated. Retry the identical host/database/user with only its password
        # changed. No fallback occurs after executing any database statement.
        return psycopg.connect(make_conninfo(database_url, password=password),
                               row_factory=dict_row, connect_timeout=10)


def recover_credentials(database_url, old_key, new_key, new_password, *, maintenance_confirmed=False):
    """Rotate both atomically; retain the same inputs if commit outcome is unknown."""
    try:
        if maintenance_confirmed is not True:
            raise RecoveryError('Maintenance confirmation required')
        if (not isinstance(new_password, str) or not 32 <= len(new_password) <= 256
                or any(not 33 <= ord(char) <= 126 for char in new_password)):
            raise RecoveryError('Replacement database password must be 32–256 printable non-space ASCII characters')
        if not isinstance(database_url, str) or not database_url or len(database_url.encode('utf-8')) > 8192:
            raise RecoveryError('Explicit database configuration required')
        parameters = conninfo_to_dict(database_url)
        if any(not parameters.get(key) for key in ('host', 'dbname', 'user', 'password')):
            raise RecoveryError('Explicit database host, database, user and password required')
        if parameters['password'] == new_password:
            raise RecoveryError('Replacement database password must differ from current configuration')
        old_cipher = Cipher(old_key)
        Cipher(new_key)  # Fail before connecting for malformed replacement keys.
        with _connect(database_url, new_password) as conn:
            conn.execute('SET LOCAL statement_timeout = 15000')
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            identity = conn.execute('SELECT session_user AS login, current_user AS active').fetchone()
            if identity['login'] != identity['active']:
                raise RecoveryError('Unexpected database role context')
            result = rotate_credentials(_TransactionRepository(conn, old_cipher), new_key,
                                        maintenance_confirmed=True)
            # libpq derives SCRAM locally: raw passwords never enter SQL text
            # (including any SQL text retained by database error logging).
            verifier = conn.pgconn.encrypt_password(new_password.encode('ascii'),
                                                      identity['login'].encode('utf-8'), b'scram-sha-256')
            conn.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(
                sql.Identifier(identity['login']), sql.Literal(verifier.decode('ascii'))))
        return result
    except Exception:  # noqa: BLE001 - DB exceptions can contain passwords and encrypted sessions.
        raise RecoveryError('Credential recovery failed; no credential values were printed') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--maintenance-confirmed', action='store_true')
    parser.add_argument('--wait', action='store_true', help='Stay idle after reporting outcome; stop the container manually')
    args = parser.parse_args(argv)
    exit_code = 0
    try:
        result = recover_credentials(os.environ.get('SH_DATABASE_URL'), os.environ.get('SH_SECRET_KEY'),
                                     os.environ.get('SH_NEXT_SECRET_KEY'),
                                     os.environ.get('SH_NEXT_DATABASE_PASSWORD'),
                                     maintenance_confirmed=args.maintenance_confirmed)
        print(json.dumps({'status':'CREDENTIAL_RECOVERY_COMPLETE', **result}), flush=True)
    except Exception:  # noqa: BLE001 - last-resort redaction for configuration and driver failures.
        print(json.dumps({'status':'CREDENTIAL_RECOVERY_FAILED'}), flush=True)
        exit_code = 1
    if args.wait:
        signal.pause()
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
