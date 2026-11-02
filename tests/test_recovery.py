"""Synthetic maintenance recovery tests, never production credentials."""
import json
import os
from uuid import uuid4

import psycopg
import pytest
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.conninfo import make_conninfo

from shopee_service import recover
from shopee_service.repository import Repository
from shopee_service.secrets import Cipher


@pytest.fixture
def recovery():
    admin = os.environ.get('SH_TEST_DATABASE_URL')
    if not admin:
        pytest.skip('SH_TEST_DATABASE_URL required')
    role = 'recovery_' + uuid4().hex
    old, new = Fernet.generate_key(), Fernet.generate_key()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role), sql.Literal('o'*40)))
        conn.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(sql.Identifier(role), sql.Identifier(role)))
    dsn = make_conninfo(admin, user=role, password='o'*40, options=f'-c search_path={role}')
    repo = Repository(dsn, Cipher(old))
    repo.migrate()
    repo.replace_account('primary', {'cookie_source':'synthetic', 'signing_state':{}})
    yield admin, role, repo, old, new
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(role)))
        conn.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def stored(admin, role):
    with psycopg.connect(admin) as conn:
        password = conn.execute('SELECT rolpassword FROM pg_authid WHERE rolname=%s',(role,)).fetchone()[0]
        credential = conn.execute(sql.SQL('SELECT credentials FROM {}.accounts').format(sql.Identifier(role))).fetchone()[0]
        return password, credential


def test_rotates_password_and_ciphertext_and_reruns(recovery):
    admin, role, repo, old, new = recovery
    before = stored(admin,role)
    result = recover.recover_credentials(repo.database_url, old, new, 'n'*40, maintenance_confirmed=True)
    assert result == {'rotated':1, 'already_current':0}
    after = stored(admin,role)
    assert before[0] != after[0]
    assert Cipher(new).decrypt(after[1]) == Cipher(old).decrypt(before[1])
    assert recover.recover_credentials(repo.database_url,old,new,'n'*40,maintenance_confirmed=True) == {'rotated':0,'already_current':1}


def test_deferred_commit_failure_rolls_back_both(recovery):
    admin,role,repo,old,new = recovery
    with repo._tx() as conn:
        conn.execute("CREATE FUNCTION reject_commit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic'; END $$")
        conn.execute('CREATE CONSTRAINT TRIGGER reject_recovery AFTER UPDATE ON accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_commit()')
    before = stored(admin,role)
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials(repo.database_url,old,new,'n'*40,maintenance_confirmed=True)
    assert stored(admin,role) == before


def test_outstanding_lease_changes_nothing(recovery):
    admin,role,repo,old,new = recovery
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET lease_token='synthetic' WHERE alias='primary'")
    before = stored(admin,role)
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials(repo.database_url,old,new,'n'*40,maintenance_confirmed=True)
    assert stored(admin,role) == before


@pytest.mark.parametrize('password',[None,'short','x'*257,'x'*39+'\n','x'*39+' '])
def test_invalid_password_never_connects(monkeypatch,password):
    monkeypatch.setattr(recover.psycopg,'connect',lambda *a,**k:pytest.fail('connected'))
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials('unused',Fernet.generate_key(),Fernet.generate_key(),password,maintenance_confirmed=True)


def test_requires_maintenance(monkeypatch):
    monkeypatch.setattr(recover.psycopg,'connect',lambda *a,**k:pytest.fail('connected'))
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials('unused',Fernet.generate_key(),Fernet.generate_key(),'x'*40)


def test_cli_redacts_exceptions(monkeypatch,capsys):
    monkeypatch.setattr(recover,'recover_credentials',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('SENSITIVE')))
    assert recover.main(['--maintenance-confirmed']) == 1
    assert 'SENSITIVE' not in capsys.readouterr().out


def test_cli_counts_only(monkeypatch,capsys):
    monkeypatch.setattr(recover,'recover_credentials',lambda *a,**k:{'rotated':2,'already_current':0})
    assert recover.main(['--maintenance-confirmed']) == 0
    assert json.loads(capsys.readouterr().out) == {'status':'CREDENTIAL_RECOVERY_COMPLETE','rotated':2,'already_current':0}


def test_fallback_changes_only_password(monkeypatch):
    calls = []
    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        if len(calls) == 1:
            raise psycopg.OperationalError('SENSITIVE')
        return 'connected'
    monkeypatch.setattr(recover.psycopg, 'connect', connect)
    assert recover._connect('host=localhost dbname=test user=synthetic password=old', 'n'*40) == 'connected'
    from psycopg.conninfo import conninfo_to_dict
    first, second = [conninfo_to_dict(call[0]) for call in calls]
    assert second == {**first, 'password':'n'*40}
    assert all(call[1]['connect_timeout'] == 10 for call in calls)


def test_wait_reports_before_idling(monkeypatch,capsys):
    monkeypatch.setattr(recover,'recover_credentials',lambda *a,**k:{'rotated':0,'already_current':1})
    waits = []
    monkeypatch.setattr(recover.signal,'pause',lambda:waits.append('paused'))
    assert recover.main(['--maintenance-confirmed','--wait']) == 0
    assert waits == ['paused']
    assert json.loads(capsys.readouterr().out)['status'] == 'CREDENTIAL_RECOVERY_COMPLETE'


def test_unknown_ciphertext_changes_neither_password_nor_data(recovery):
    admin,role,repo,old,new = recovery
    with repo._tx() as conn:
        conn.execute("UPDATE accounts SET credentials='synthetic-invalid'")
    before = stored(admin,role)
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials(repo.database_url,old,new,'n'*40,maintenance_confirmed=True)
    assert stored(admin,role) == before


@pytest.mark.parametrize('dsn',[None,'','host=localhost','dbname=test user=x password=y',
    'host=localhost dbname=test user=x','x'*8193])
def test_missing_or_oversized_dsn_never_uses_libpq_defaults(monkeypatch,dsn):
    monkeypatch.setattr(recover.psycopg,'connect',lambda *a,**k:pytest.fail('connected'))
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials(dsn,Fernet.generate_key(),Fernet.generate_key(),'n'*40,maintenance_confirmed=True)


def test_same_password_rejected_before_connection(monkeypatch):
    monkeypatch.setattr(recover.psycopg,'connect',lambda *a,**k:pytest.fail('connected'))
    dsn = make_conninfo(host='localhost',dbname='test',user='synthetic',password='n'*40)
    with pytest.raises(recover.RecoveryError):
        recover.recover_credentials(dsn,Fernet.generate_key(),Fernet.generate_key(),'n'*40,maintenance_confirmed=True)
