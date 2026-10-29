"""Atomic maintenance-only encryption-key rotation for stored account sessions.

Operators must stop every API, worker and account-administration process before
running this operation, and keep them stopped until their configured key matches
the new key. Database locks coordinate transactions, not process configuration.
"""
from __future__ import annotations

from .secrets import Cipher, SecretError


class RotationError(ValueError):
    """Safe operational failure; never contains keys, ciphertext or session data."""


def _decrypt(cipher, value):
    try:
        return cipher.decrypt(value)
    except SecretError:
        return None


def _classify(rows, old_cipher, new_cipher):
    old_rows, new_count = [], 0
    for row in rows:
        old = _decrypt(old_cipher, row['credentials'])
        new = _decrypt(new_cipher, row['credentials'])
        if old is not None and new is None:
            old_rows.append((row['alias'], old))
        elif new is not None and old is None:
            new_count += 1
        else:
            raise RotationError('Stored session encryption could not be classified; no changes applied')
    if old_rows and new_count:
        raise RotationError('Mixed encryption keys detected; no changes applied')
    return old_rows, new_count


def rotate_credentials(repo, new_key: str | bytes, *, maintenance_confirmed: bool = False) -> dict[str, int]:
    """Re-encrypt all rows atomically, or recognize an already-completed rerun.

    The repository's cipher is the old SH_SECRET_KEY. A rerun with the same old
    configuration and new-key file succeeds only when *every* row uses the new
    key. Mixed, unknown and damaged ciphertext are never silently skipped.
    """
    if maintenance_confirmed is not True:
        raise RotationError('Confirm maintenance with all API and worker processes stopped')
    new_cipher = Cipher(new_key)
    if _decrypt(new_cipher, repo.cipher.encrypt({'key_check': True})) is not None:
        raise RotationError('The new encryption key must be different from the current key')
    with repo._tx() as conn:
        # _tx holds the same transaction advisory lock as all repository writes.
        rows = conn.execute('SELECT alias,credentials FROM accounts ORDER BY alias FOR UPDATE').fetchall()
        leased = conn.execute("""SELECT
            EXISTS(SELECT 1 FROM accounts WHERE lease_token IS NOT NULL OR lease_until IS NOT NULL)
            OR EXISTS(SELECT 1 FROM jobs WHERE status='leased' OR lease_token IS NOT NULL OR lease_until>now())
            OR EXISTS(SELECT 1 FROM alert_outbox WHERE lease_token IS NOT NULL OR lease_until>now())
            AS present""").fetchone()['present']
        if leased:
            raise RotationError('Outstanding lease markers exist; no changes applied')
        old_rows, new_count = _classify(rows, repo.cipher, new_cipher)
        for alias, plaintext in old_rows:
            ciphertext = new_cipher.encrypt(plaintext)
            if new_cipher.decrypt(ciphertext) != plaintext:
                raise RotationError('Encryption verification failed; no changes applied')
            conn.execute('UPDATE accounts SET credentials=%s WHERE alias=%s', (ciphertext, alias))
        # Verify the actual stored values inside the transaction before commit.
        stored = conn.execute('SELECT alias,credentials FROM accounts ORDER BY alias').fetchall()
        expected = dict(old_rows)
        for row in stored:
            plaintext = new_cipher.decrypt(row['credentials'])
            if row['alias'] in expected and plaintext != expected[row['alias']]:
                raise RotationError('Stored encryption verification failed; no changes applied')
        return {'rotated': len(old_rows), 'already_current': new_count}
