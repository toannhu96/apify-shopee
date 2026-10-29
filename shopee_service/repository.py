"""PostgreSQL durable queue, account fencing, cache and delivery journal.

All modifying transactions share a short advisory lock. No network call occurs
inside these transactions. This deliberately favors auditable coordination over
maximum database throughput for the initial small account pool.
"""
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

LOGGER = logging.getLogger(__name__)

LOCK_ID = 628941032
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
ALIAS = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
ITEM_ID = re.compile(r'[1-9][0-9]{0,19}\Z')
FAILURES = frozenset({'authentication', 'verification', 'rate_limit', 'transient',
                      'unavailable', 'invalid_response', 'signing', 'session_unavailable'})


class Conflict(ValueError):
    """The request conflicts with an existing durable operation."""


class Missing(LookupError):
    """The requested operation does not exist."""


@dataclass(frozen=True)
class Reservation:
    item_id: str
    account_alias: str
    account_version: int
    token: str
    attempt: int
    headers: dict = field(repr=False)
    cookie_source: str = field(repr=False)
    proxy_url: str | None = field(repr=False)
    verification: bool = False


def _id(value, pattern=SAFE_ID):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError('Invalid identifier')
    return value


class Repository:
    def __init__(self, database_url, cipher, *, min_interval_seconds=6, lease_seconds=90):
        self.database_url = database_url
        self.cipher = cipher
        self.interval = max(6, min_interval_seconds)
        self.lease = max(60, lease_seconds)

    @contextmanager
    def _tx(self):
        with psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=10) as conn:
            conn.execute('SET LOCAL statement_timeout = 15000')
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            yield conn

    def health(self):
        with self._tx() as conn:
            return conn.execute('SELECT 1 FROM runs LIMIT 1').description is not None

    def migrate(self):
        sql = Path(__file__).with_name('migrations').joinpath('001_initial.sql').read_text()
        with self._tx() as conn:
            conn.execute(sql)

    def _terminal(self, conn, run_id, item_id, status, *, result=None, error=None):
        pending = conn.execute("SELECT 1 FROM run_items WHERE run_id=%s AND item_id=%s AND status='pending'",
                               (run_id, item_id)).fetchone()
        if not pending:
            return
        sequence = conn.execute('UPDATE runs SET next_sequence=next_sequence+1 WHERE run_id=%s RETURNING next_sequence',
                                (run_id,)).fetchone()['next_sequence']
        conn.execute('UPDATE run_items SET status=%s,sequence=%s,result=%s,error_code=%s WHERE run_id=%s AND item_id=%s',
                     (status, sequence, Jsonb(result) if result is not None else None, error, run_id, item_id))
        conn.execute("UPDATE runs SET status='completed',completed_at=now() WHERE run_id=%s AND status='active' "
                     "AND NOT EXISTS(SELECT 1 FROM run_items WHERE run_id=%s AND status='pending')", (run_id, run_id))

    def _fanout(self, conn, item_id, status, *, result=None, error=None):
        rows = conn.execute("SELECT i.run_id FROM run_items i JOIN runs r USING(run_id) "
                            "WHERE i.item_id=%s AND i.status='pending' AND r.status='active' ORDER BY i.run_id",
                            (item_id,)).fetchall()
        for row in rows:
            self._terminal(conn, row['run_id'], item_id, status, result=result, error=error)

    def _maintenance(self, conn):
        expired = conn.execute("UPDATE runs SET status='expired',completed_at=now() "
                               "WHERE status='active' AND deadline_at<=now() RETURNING run_id").fetchall()
        for row in expired:
            self._stop_items(conn, row['run_id'], 'deadline_exceeded')
        orphaned = conn.execute("SELECT item_id FROM jobs j WHERE NOT EXISTS "
                                "(SELECT 1 FROM run_items i JOIN runs r USING(run_id) WHERE i.item_id=j.item_id "
                                "AND i.status='pending' AND r.status='active')").fetchall()
        for row in orphaned:
            # Keep active reservations until their bounded request has finished.
            conn.execute("DELETE FROM jobs WHERE item_id=%s AND (status='pending' OR lease_until<=now())", (row['item_id'],))
        conn.execute("UPDATE accounts SET status='healthy' WHERE status='rate_limit' AND next_request_at<=now()")
        stale = conn.execute("SELECT * FROM jobs WHERE status='leased' AND lease_until<=now()").fetchall()
        for row in stale:
            if row['attempts'] >= 2:
                self._fanout(conn, row['item_id'], 'failed', error='transient')
                conn.execute('DELETE FROM jobs WHERE item_id=%s', (row['item_id'],))
            else:
                conn.execute("UPDATE jobs SET status='pending',lease_token=NULL,available_at=now() WHERE item_id=%s", (row['item_id'],))
        conn.execute('UPDATE accounts SET lease_token=NULL,lease_until=NULL WHERE lease_until<=now()')

    def _stop_items(self, conn, run_id, reason):
        for row in conn.execute("SELECT item_id FROM run_items WHERE run_id=%s AND status='pending' ORDER BY position", (run_id,)).fetchall():
            self._terminal(conn, run_id, row['item_id'], 'unfinished', error=reason)

    def _unavailable(self, conn):
        available = conn.execute("SELECT 1 FROM accounts WHERE status IN ('healthy','rate_limit') LIMIT 1").fetchone()
        if available:
            return
        for row in conn.execute("SELECT run_id,item_id FROM run_items WHERE status='pending' ORDER BY run_id,position").fetchall():
            self._terminal(conn, row['run_id'], row['item_id'], 'failed', error='session_unavailable')

    def submit_run(self, run_id, product_ids, deadline_at):
        _id(run_id)
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 1000:
            raise ValueError('Expected 1 to 1000 unique products')
        ids = [_id(item, ITEM_ID) for item in product_ids]
        if len(set(ids)) != len(ids):
            raise ValueError('Products must be deduplicated')
        if not isinstance(deadline_at, datetime) or deadline_at.tzinfo is None:
            raise ValueError('Deadline must include a timezone')
        with self._tx() as conn:
            old = conn.execute('SELECT * FROM runs WHERE run_id=%s', (run_id,)).fetchone()
            if old:
                if old['product_ids'] != ids or old['deadline_at'] != deadline_at:
                    raise Conflict('Run input cannot change')
            else:
                now = conn.execute('SELECT now() AS now').fetchone()['now']
                if not now < deadline_at <= now + timedelta(hours=2, seconds=2):
                    raise ValueError('Deadline must be within two hours')
                conn.execute('INSERT INTO runs(run_id,product_ids,deadline_at) VALUES(%s,%s,%s)',
                             (run_id, Jsonb(ids), deadline_at))
                for position, item_id in enumerate(ids):
                    conn.execute('INSERT INTO run_items(run_id,item_id,position) VALUES(%s,%s,%s)', (run_id, item_id, position))
                for item_id in ids:
                    cached = conn.execute('SELECT c.result FROM product_cache c JOIN accounts a ON a.alias=c.account_alias '
                                          'AND a.version=c.account_version WHERE c.item_id=%s AND c.expires_at>now()', (item_id,)).fetchone()
                    if cached:
                        self._terminal(conn, run_id, item_id, 'succeeded', result={**cached['result'], 'cacheHit': True})
                    else:
                        conn.execute('INSERT INTO jobs(item_id) VALUES(%s) ON CONFLICT DO NOTHING', (item_id,))
            self._maintenance(conn)
            self._unavailable(conn)
            return self._snapshot(conn, run_id, 0, 100)

    def _snapshot(self, conn, run_id, cursor, limit):
        run = conn.execute('SELECT * FROM runs WHERE run_id=%s', (run_id,)).fetchone()
        if not run:
            raise Missing('Run not found')
        rows = conn.execute('SELECT item_id,status,sequence,result,error_code FROM run_items '
                            'WHERE run_id=%s AND sequence>%s ORDER BY sequence LIMIT %s', (run_id, cursor, limit)).fetchall()
        counts = {name: 0 for name in ('succeeded', 'failed', 'unfinished', 'pending')}
        for row in conn.execute('SELECT status,count(*) AS count FROM run_items WHERE run_id=%s GROUP BY status', (run_id,)).fetchall():
            counts[row['status']] = row['count']
        results = [{'sequence': row['sequence'], 'itemId': row['item_id'], 'status': row['status'],
                    'result': row['result'], 'errorCode': row['error_code']} for row in rows]
        return {'runId': run_id, 'status': run['status'], 'results': results,
                'nextCursor': rows[-1]['sequence'] if rows else cursor,
                'done': counts['pending'] == 0, 'counts': counts}

    def get_run(self, run_id, *, cursor=0, limit=100):
        _id(run_id)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Invalid pagination')
        with self._tx() as conn:
            self._maintenance(conn)
            self._unavailable(conn)
            return self._snapshot(conn, run_id, cursor, limit)

    def cancel_run(self, run_id, reason='cancelled'):
        _id(run_id)
        reason = reason if reason in ('cancelled', 'budget_exhausted', 'deadline_exceeded', 'billing_uncertain') else 'cancelled'
        with self._tx() as conn:
            run = conn.execute('SELECT status FROM runs WHERE run_id=%s', (run_id,)).fetchone()
            if not run:
                raise Missing('Run not found')
            if run['status'] == 'active':
                conn.execute("UPDATE runs SET status='cancelled',completed_at=now() WHERE run_id=%s", (run_id,))
                self._stop_items(conn, run_id, reason)
            return self._snapshot(conn, run_id, 0, 100)

    def replace_account(self, alias, credentials, *, create_only=False, require_existing=False):
        _id(alias, ALIAS)
        if type(create_only) is not bool or type(require_existing) is not bool or (create_only and require_existing):
            raise ValueError('Invalid account replacement mode')
        encrypted = self.cipher.encrypt(credentials)
        with self._tx() as conn:
            exists = conn.execute('SELECT 1 FROM accounts WHERE alias=%s FOR UPDATE', (alias,)).fetchone()
            if create_only and exists:
                raise Conflict('Account alias already exists')
            if require_existing and not exists:
                raise Missing('Account alias does not exist')
            row = conn.execute("INSERT INTO accounts(alias,status,credentials) VALUES(%s,'pending',%s) "
                               "ON CONFLICT(alias) DO UPDATE SET version=accounts.version+1,status='pending',"
                               "credentials=EXCLUDED.credentials,updated_at=now(),next_request_at=GREATEST(accounts.next_request_at,accounts.lease_until,now()),lease_token=NULL,lease_until=NULL "
                               'RETURNING version', (alias, encrypted)).fetchone()
            conn.execute('DELETE FROM product_cache WHERE account_alias=%s', (alias,))
            conn.execute("UPDATE jobs SET status='pending',lease_token=NULL,available_at=GREATEST(now(),lease_until) "
                         'WHERE account_alias=%s', (alias,))
            return row['version']

    def disable_account(self, alias):
        _id(alias, ALIAS)
        with self._tx() as conn:
            row = conn.execute("UPDATE accounts SET status='disabled',updated_at=now() WHERE alias=%s RETURNING alias", (alias,)).fetchone()
            if not row:
                raise Missing('Account not found')

    def list_accounts(self):
        with self._tx() as conn:
            return conn.execute('SELECT alias,version,status,next_request_at,updated_at FROM accounts ORDER BY alias').fetchall()

    def _reserve(self, conn, account, item_id, reserve, attempt, *, verification=False):
        credential = self.cipher.decrypt(account['credentials'])
        headers, signing_state = reserve(item_id, credential['signing_state'])
        token = uuid4().hex
        encrypted = self.cipher.encrypt({**credential, 'signing_state': signing_state})
        conn.execute('UPDATE accounts SET credentials=%s,lease_token=%s,lease_until=now()+%s*interval\'1 second\', '
                     'next_request_at=now()+%s*interval\'1 second\' WHERE alias=%s',
                     (encrypted, token, self.lease, self.interval, account['alias']))
        return Reservation(item_id, account['alias'], account['version'], token, attempt, headers,
                           credential['cookie_source'], credential.get('proxy_url'), verification)

    def claim_job(self, *, reserve=None):
        if reserve is None:
            from shopee_affiliate.session_signer import reserve_request
            reserve = reserve_request
        with self._tx() as conn:
            self._maintenance(conn)
            self._unavailable(conn)
            account = conn.execute("SELECT * FROM accounts WHERE status='healthy' AND next_request_at<=now() "
                                   'AND lease_token IS NULL ORDER BY next_request_at,alias LIMIT 1').fetchone()
            if not account:
                return None
            job = conn.execute("SELECT j.*,r.run_id FROM jobs j JOIN run_items i USING(item_id) JOIN runs r USING(run_id) "
                               "WHERE j.status='pending' AND j.available_at<=now() AND i.status='pending' AND r.status='active' "
                               'ORDER BY r.last_dispatched_at,r.created_at,i.position LIMIT 1').fetchone()
            if not job:
                return None
            try:
                work = self._reserve(conn, account, job['item_id'], reserve, job['attempts'] + 1)
            except (ValueError, KeyError, TypeError):
                LOGGER.warning('Shopee failure phase=job_reservation category=signing')
                self._incident(conn, account['alias'], 'verification')
                return None
            conn.execute("UPDATE jobs SET status='leased',attempts=%s,account_alias=%s,account_version=%s,lease_token=%s,"
                         "lease_until=now()+%s*interval '1 second' WHERE item_id=%s",
                         (work.attempt, work.account_alias, work.account_version, work.token, self.lease, work.item_id))
            conn.execute('UPDATE runs SET last_dispatched_at=now() WHERE run_id=%s', (job['run_id'],))
            return work

    def _current(self, conn, work):
        account = conn.execute('SELECT * FROM accounts WHERE alias=%s AND version=%s AND lease_token=%s AND lease_until>now()',
                               (work.account_alias, work.account_version, work.token)).fetchone()
        if not account:
            return False
        if work.verification:
            return account['status'] not in ('disabled',)
        return bool(conn.execute("SELECT 1 FROM jobs WHERE item_id=%s AND status='leased' AND lease_token=%s AND lease_until>now()",
                                 (work.item_id, work.token)).fetchone()) and account['status'] != 'disabled'

    def reservation_current(self, work):
        """Check immediately before network dispatch, including cancellation.

        The remaining lease must exceed the transport's 20-second timeout.
        This catches workers resumed after a pause; completion is fenced again.
        """
        with self._tx() as conn:
            self._maintenance(conn)
            if not self._current(conn, work):
                return False
            enough_time = conn.execute(
                "SELECT 1 FROM accounts WHERE alias=%s AND lease_until>now()+interval '35 seconds'",
                (work.account_alias,),
            ).fetchone()
            if not enough_time:
                return False
            if work.verification:
                return True
            pending = conn.execute(
                "SELECT 1 FROM run_items i JOIN runs r USING(run_id) WHERE i.item_id=%s "
                "AND i.status='pending' AND r.status='active' LIMIT 1", (work.item_id,),
            ).fetchone()
            if not pending:
                self._release(conn, work)
                conn.execute('DELETE FROM jobs WHERE item_id=%s AND lease_token=%s', (work.item_id, work.token))
                return False
            return True

    def _release(self, conn, work):
        conn.execute('UPDATE accounts SET lease_token=NULL,lease_until=NULL WHERE alias=%s AND lease_token=%s',
                     (work.account_alias, work.token))

    def _record_response(self, conn, work):
        """Advance completed-response counters inside the fenced outcome transaction."""
        from shopee_affiliate.session_signer import record_response
        row = conn.execute('SELECT credentials FROM accounts WHERE alias=%s', (work.account_alias,)).fetchone()
        credentials = self.cipher.decrypt(row['credentials'])
        updated = {**credentials, 'signing_state': record_response(credentials['signing_state'])}
        conn.execute('UPDATE accounts SET credentials=%s WHERE alias=%s',
                     (self.cipher.encrypt(updated), work.account_alias))

    def complete_job(self, work, result, *, response_received=False):
        with self._tx() as conn:
            self._maintenance(conn)
            if not self._current(conn, work):
                return False
            if response_received is True:
                self._record_response(conn, work)
            conn.execute('INSERT INTO product_cache(item_id,account_alias,account_version,result,expires_at) '
                         "VALUES(%s,%s,%s,%s,now()+interval '15 minutes') ON CONFLICT(item_id) DO UPDATE SET "
                         'account_alias=EXCLUDED.account_alias,account_version=EXCLUDED.account_version,result=EXCLUDED.result,'
                         'expires_at=EXCLUDED.expires_at,created_at=now()',
                         (work.item_id, work.account_alias, work.account_version, Jsonb(result)))
            self._fanout(conn, work.item_id, 'succeeded', result=result)
            conn.execute('DELETE FROM jobs WHERE item_id=%s', (work.item_id,))
            self._release(conn, work)
            return True

    def _alert(self, conn, key, content, occurred_at):
        conn.execute('INSERT INTO alert_outbox(dedupe_key,payload) VALUES(%s,%s) ON CONFLICT DO NOTHING',
                     (key, Jsonb({'content': content + ' Time: ' + occurred_at.astimezone(UTC).isoformat(), 'allowed_mentions': {'parse': []}})))

    def _incident(self, conn, alias, code):
        old = conn.execute('SELECT * FROM accounts WHERE alias=%s', (alias,)).fetchone()
        status = code if code in ('authentication', 'rate_limit') else 'verification'
        increment = 0 if old['status'] == status else 1
        row = conn.execute('UPDATE accounts SET status=%s,incident=incident+%s,updated_at=now() WHERE alias=%s RETURNING *',
                           (status, increment, alias)).fetchone()
        action = 'Refresh the Shopee session using the server CLI.'
        if status == 'verification':
            action = 'Check the account in Shopee, complete verification if requested, then replace its session.'
        elif status == 'rate_limit':
            action = 'Account cooling down for 15 minutes; no session refresh is assumed.'
            conn.execute("UPDATE accounts SET next_request_at=now()+interval '15 minutes' WHERE alias=%s", (alias,))
        self._alert(conn, f'{alias}:{row["version"]}:{row["incident"]}:{status}',
                    f'Shopee account {alias}: {status}. {action}', row['updated_at'])

    def fail_job(self, work, code, *, response_received=False):
        code = code if code in FAILURES else 'invalid_response'
        with self._tx() as conn:
            self._maintenance(conn)
            if not self._current(conn, work):
                return False
            if response_received is True:
                self._record_response(conn, work)
            self._release(conn, work)
            if code in ('authentication', 'verification', 'signing', 'rate_limit'):
                self._incident(conn, work.account_alias, code)
                # Another healthy account can take the same item; block incidents
                # do not consume the single allowed transient retry.
                conn.execute("UPDATE jobs SET status='pending',lease_token=NULL,attempts=GREATEST(0,attempts-1),available_at=now() "
                             'WHERE item_id=%s', (work.item_id,))
                self._unavailable(conn)
            elif code == 'transient' and work.attempt < 2:
                conn.execute("UPDATE jobs SET status='pending',lease_token=NULL,available_at=now()+interval '5 seconds' WHERE item_id=%s", (work.item_id,))
            else:
                self._fanout(conn, work.item_id, 'failed', error=code)
                conn.execute('DELETE FROM jobs WHERE item_id=%s', (work.item_id,))
            return True

    def claim_verification(self, alias, item_id, *, reserve=None):
        _id(alias, ALIAS)
        _id(item_id, ITEM_ID)
        if reserve is None:
            from shopee_affiliate.session_signer import reserve_request
            reserve = reserve_request
        with self._tx() as conn:
            self._maintenance(conn)
            account = conn.execute('SELECT * FROM accounts WHERE alias=%s', (alias,)).fetchone()
            if not account:
                raise Missing('Account not found')
            if account['lease_token'] or account['next_request_at'] > datetime.now(UTC):
                return None
            if account['status'] == 'disabled':
                raise Conflict('Replace the disabled session before verifying')
            try:
                return self._reserve(conn, account, item_id, reserve, 1, verification=True)
            except (ValueError, KeyError, TypeError):
                LOGGER.warning('Shopee failure phase=verification_reservation category=signing')
                raise

    def complete_verification(self, work, *, response_received=False):
        with self._tx() as conn:
            if not self._current(conn, work):
                return False
            if response_received is True:
                self._record_response(conn, work)
            row = conn.execute("UPDATE accounts SET status='healthy',updated_at=now() WHERE alias=%s RETURNING updated_at", (work.account_alias,)).fetchone()
            self._release(conn, work)
            self._alert(conn, f'{work.account_alias}:{work.account_version}:recovery',
                        f'Shopee account {work.account_alias}: session validated and available.', row['updated_at'])
            return True

    def fail_verification(self, work, code, *, response_received=False):
        with self._tx() as conn:
            if not self._current(conn, work):
                return False
            if response_received is True:
                self._record_response(conn, work)
            self._release(conn, work)
            if code in ('authentication', 'verification', 'signing', 'rate_limit'):
                self._incident(conn, work.account_alias, code)
            return True

    def record_delivery(self, run_id, item_id, phase, result_id):
        _id(run_id)
        _id(item_id, ITEM_ID)
        if not isinstance(result_id, str) or not 1 <= len(result_id) <= 256 or any(ord(x) < 32 for x in result_id):
            raise ValueError('Invalid result ID')
        transitions = {'delivering': {'delivered', 'uncertain'}, 'delivered': {'charging', 'uncertain'},
                       'charging': {'charged', 'uncertain'}, 'charged': set(), 'uncertain': set()}
        if phase not in transitions:
            raise ValueError('Invalid delivery phase')
        with self._tx() as conn:
            item = conn.execute('SELECT status FROM run_items WHERE run_id=%s AND item_id=%s', (run_id, item_id)).fetchone()
            if not item:
                raise Missing('Product operation not found')
            if item['status'] != 'succeeded':
                raise Conflict('Only successful products can be delivered')
            old = conn.execute('SELECT * FROM deliveries WHERE run_id=%s AND item_id=%s', (run_id, item_id)).fetchone()
            if old:
                if old['result_id'] != result_id or (old['phase'] != phase and phase not in transitions[old['phase']]):
                    raise Conflict('Delivery state cannot regress or change identity')
            elif phase not in ('delivering', 'delivered', 'uncertain'):
                raise Conflict('Delivery must precede billing')
            row = conn.execute('INSERT INTO deliveries(run_id,item_id,result_id,phase) VALUES(%s,%s,%s,%s) '
                               'ON CONFLICT(run_id,item_id) DO UPDATE SET phase=EXCLUDED.phase,updated_at=now() RETURNING *',
                               (run_id, item_id, result_id, phase)).fetchone()
            return {'itemId': item_id, 'resultId': result_id, 'phase': row['phase'], 'updatedAt': row['updated_at'].isoformat()}

    def get_deliveries(self, run_id):
        _id(run_id)
        with self._tx() as conn:
            if not conn.execute('SELECT 1 FROM runs WHERE run_id=%s', (run_id,)).fetchone():
                raise Missing('Run not found')
            rows = conn.execute('SELECT * FROM deliveries WHERE run_id=%s ORDER BY item_id', (run_id,)).fetchall()
            return {'deliveries': [{'itemId': r['item_id'], 'resultId': r['result_id'], 'phase': r['phase'],
                                    'updatedAt': r['updated_at'].isoformat()} for r in rows]}

    def claim_alert(self):
        with self._tx() as conn:
            row = conn.execute('SELECT * FROM alert_outbox WHERE delivered_at IS NULL AND attempts<8 AND available_at<=now() '
                               'AND (lease_token IS NULL OR lease_until<=now()) ORDER BY id LIMIT 1').fetchone()
            if not row:
                return None
            token = uuid4().hex
            conn.execute("UPDATE alert_outbox SET lease_token=%s,lease_until=now()+interval '60 seconds',attempts=attempts+1 WHERE id=%s", (token, row['id']))
            return {**row, 'attempts': row['attempts'] + 1, 'lease_token': token}

    def finish_alert(self, alert_id, token, error=None, retry_after=None):
        with self._tx() as conn:
            if error is None:
                conn.execute('UPDATE alert_outbox SET delivered_at=now(),lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s', (alert_id, token))
            elif error == 'delivery_rejected':
                conn.execute('UPDATE alert_outbox SET attempts=8,lease_token=NULL,lease_until=NULL,last_error=%s WHERE id=%s AND lease_token=%s', ('delivery_rejected', alert_id, token))
            else:
                delay = min(86400, max(1, float(retry_after or 60)))
                conn.execute('UPDATE alert_outbox SET lease_token=NULL,lease_until=NULL,last_error=%s,'
                             "available_at=now()+%s*interval '1 second' WHERE id=%s AND lease_token=%s",
                             ('notification_failed', delay, alert_id, token))

    def cleanup(self):
        with self._tx() as conn:
            self._maintenance(conn)
            conn.execute("DELETE FROM runs WHERE status<>'active' AND completed_at<now()-interval '7 days'")
            conn.execute('DELETE FROM product_cache WHERE expires_at<=now()')
            conn.execute("DELETE FROM alert_outbox WHERE created_at<now()-interval '7 days' AND (delivered_at IS NOT NULL OR attempts>=8)")
