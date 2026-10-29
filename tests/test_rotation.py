"""Maintenance rekey tests use only synthetic keys and isolated PostgreSQL schemas."""
import json
import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import make_conninfo

from shopee_service import cli
from shopee_service.repository import LOCK_ID, Repository
from shopee_service.rotate import RotationError, rotate_credentials
from shopee_service.secrets import Cipher, SecretError

SENTINEL = 'SYNTHETIC_PRIVATE_SESSION_NOT_FOR_LOGS'


@pytest.fixture
def setup():
    dsn = os.environ.get('SH_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('SH_TEST_DATABASE_URL required')
    schema = 'rotation_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()
    repo = Repository(make_conninfo(dsn, options=f'-c search_path={schema}'), Cipher(old_key))
    repo.migrate()
    for alias in ('first', 'second'):
        repo.replace_account(alias, {'cookie_source': SENTINEL, 'signing_state': {'count': 9}, 'proxy_url': None})
    yield repo, new_key
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def rows(repo):
    with repo._tx() as conn:
        return conn.execute('SELECT * FROM accounts ORDER BY alias').fetchall()


def test_atomic_rotation_preserves_metadata_and_safe_rerun(setup):
    repo, key = setup
    before = rows(repo)
    assert rotate_credentials(repo, key, maintenance_confirmed=True) == {'rotated': 2, 'already_current': 0}
    after = rows(repo)
    for old, new in zip(before, after, strict=True):
        assert {k: v for k, v in old.items() if k != 'credentials'} == {k: v for k, v in new.items() if k != 'credentials'}
        assert Cipher(key).decrypt(new['credentials']) == repo.cipher.decrypt(old['credentials'])
        with pytest.raises(SecretError):
            repo.cipher.decrypt(new['credentials'])
    assert rotate_credentials(repo, key, maintenance_confirmed=True) == {'rotated': 0, 'already_current': 2}
    assert rows(repo) == after


@pytest.mark.parametrize('kind', ['account', 'job', 'pending_job', 'alert', 'pending_alert'])
def test_refuses_any_lease_marker_including_expired(setup, kind):
    repo, key = setup
    with repo._tx() as conn:
        if kind == 'account':
            conn.execute("UPDATE accounts SET lease_token='synthetic', lease_until=now()-interval '1 hour' WHERE alias='first'")
        elif kind == 'job':
            conn.execute("INSERT INTO jobs(item_id,status) VALUES('123','leased')")
        elif kind == 'pending_job':
            conn.execute("INSERT INTO jobs(item_id,status,lease_until) VALUES('123','pending',now()+interval '1 hour')")
        elif kind == 'pending_alert':
            conn.execute("INSERT INTO alert_outbox(dedupe_key,payload,lease_until) VALUES('synthetic','{}',now()+interval '1 hour')")
        else:
            conn.execute("INSERT INTO alert_outbox(dedupe_key,payload,lease_token) VALUES('synthetic','{}','synthetic')")
    before = rows(repo)
    with pytest.raises(RotationError, match='lease'):
        rotate_credentials(repo, key, maintenance_confirmed=True)
    assert rows(repo) == before


@pytest.mark.parametrize('ciphertext', ['new', 'unknown', 'malformed'])
def test_mixed_or_unknown_encryption_refused_without_changes(setup, ciphertext):
    repo, key = setup
    token = (Cipher(key if ciphertext == 'new' else Fernet.generate_key()).encrypt({'secret': SENTINEL})
             if ciphertext != 'malformed' else SENTINEL)
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET credentials=%s WHERE alias='second'", (token,))
    before = rows(repo)
    with pytest.raises(RotationError):
        rotate_credentials(repo, key, maintenance_confirmed=True)
    assert rows(repo) == before


def test_failure_on_second_update_rolls_back_first(setup):
    repo, key = setup
    with repo._tx() as conn:
        conn.execute("""CREATE FUNCTION reject_second() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN IF NEW.alias='second' THEN RAISE EXCEPTION 'synthetic failure'; END IF; RETURN NEW; END $$""")
        conn.execute('CREATE TRIGGER fail_rotation BEFORE UPDATE ON accounts FOR EACH ROW EXECUTE FUNCTION reject_second()')
    before = rows(repo)
    with pytest.raises(psycopg.errors.RaiseException):
        rotate_credentials(repo, key, maintenance_confirmed=True)
    assert rows(repo) == before


def test_requires_maintenance_and_distinct_key(setup):
    repo, key = setup
    before = rows(repo)
    with pytest.raises(RotationError, match='maintenance'):
        rotate_credentials(repo, key)
    # Use a cipher encrypting with the target key as current configuration.
    repo.cipher = Cipher(key)
    with pytest.raises(RotationError, match='different'):
        rotate_credentials(repo, key, maintenance_confirmed=True)
    assert rows(repo) == before


def test_repository_advisory_lock_held_during_decryption(setup, monkeypatch):
    repo, key = setup
    original = repo.cipher.decrypt
    def checked(value):
        with psycopg.connect(repo.database_url) as conn:
            assert conn.execute('SELECT pg_try_advisory_xact_lock(%s)', (LOCK_ID,)).fetchone()[0] is False
        return original(value)
    monkeypatch.setattr(repo.cipher, 'decrypt', checked)
    assert rotate_credentials(repo, key, maintenance_confirmed=True)['rotated'] == 2


def test_cli_private_file_and_counts_only(setup, tmp_path, monkeypatch, capsys):
    repo, key = setup
    path = tmp_path / 'new-key'
    path.write_bytes(key + b'\n')
    path.chmod(0o600)
    monkeypatch.setattr(cli, '_repository', lambda: repo)
    assert cli.main(['rotate-secret-key', '--new-key-file', str(path), '--maintenance-confirmed']) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {'rotated': 2, 'already_current': 0}
    assert not output.err and SENTINEL not in output.out and key.decode() not in output.out


def test_cli_rejects_insecure_key_file_and_redacts_errors(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'new-key'
    key = Fernet.generate_key()
    path.write_bytes(key)
    path.chmod(0o644)
    monkeypatch.setattr(cli, '_repository', lambda: object())
    assert cli.main(['rotate-secret-key', '--new-key-file', str(path), '--maintenance-confirmed']) == 1
    output = capsys.readouterr()
    assert key.decode() not in output.err and not output.out
