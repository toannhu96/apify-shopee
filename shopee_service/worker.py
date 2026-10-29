"""Bounded synchronous worker; one process handles at most one account request.

Run additional worker replicas to use independent accounts concurrently. The
repository fences all reservations, so replicas share pacing and signing state.
"""
import logging
import signal
import threading
import time
from datetime import UTC, datetime

from shopee_affiliate.direct import DirectError
from shopee_affiliate.signed_client import fetch_signed_product
from shopee_contracts import normalize_output

from .alerts import dispatch_one
from .config import Settings
from .repository import FAILURES, Repository
from .secrets import Cipher

LOGGER = logging.getLogger(__name__)


def _log_failure(phase, error=None):
    category = getattr(error, 'category', 'invalid_response')
    category = category if isinstance(category, str) and category in FAILURES | {'input'} else 'invalid_response'
    status, code = getattr(error, 'http_status', None), getattr(error, 'api_code', None)
    status = status if type(status) is int and 100 <= status <= 599 else None
    code = code if type(code) is int and abs(code) < 10**10 else None
    LOGGER.warning('Shopee failure phase=%s category=%s http_status=%s api_code=%s',
                   phase, category, status, code)


def _fetch(work, fetch):
    return fetch(work.item_id, headers=work.headers, cookie_source=work.cookie_source,
                 proxy_url=work.proxy_url)


def process_one(repo, *, fetch=fetch_signed_product):
    work = repo.claim_job()
    if work is None:
        return False
    if not repo.reservation_current(work):
        return True
    received = False
    phase = 'job_fetch'
    try:
        response = _fetch(work, fetch)
        received = True
        phase = 'job_result'
        result = normalize_output(response['product'], fetched_at=datetime.now(UTC), cache_hit=False)
    except DirectError as error:
        _log_failure(phase, error)
        received = received or error.response_received is True
        code = error.category if error.category != 'input' else 'signing'
        repo.fail_job(work, code, response_received=received)
    except Exception:  # noqa: BLE001 - never leak credentials in third-party errors
        # Response parsing and third-party exceptions must never expose headers,
        # response bodies or proxy URLs in logs.
        _log_failure(phase)
        repo.fail_job(work, 'invalid_response', response_received=received)
    else:
        repo.complete_job(work, result, response_received=received)
    return True


def verify_account(repo, alias, item_id, *, fetch=fetch_signed_product):
    work = repo.claim_verification(alias, item_id)
    if work is None or not repo.reservation_current(work):
        return False
    received = False
    phase = 'verification_fetch'
    try:
        response = _fetch(work, fetch)
        received = True
        phase = 'verification_result'
        normalize_output(response['product'], fetched_at=datetime.now(UTC), cache_hit=False)
    except DirectError as error:
        _log_failure(phase, error)
        received = received or error.response_received is True
        repo.fail_verification(work, error.category, response_received=received)
        return False
    except Exception:  # noqa: BLE001 - never leak credentials in third-party errors
        _log_failure(phase)
        repo.fail_verification(work, 'invalid_response', response_received=received)
        return False
    return repo.complete_verification(work, response_received=received)


def run_loop(repo, settings, stop):
    last_cleanup = float('-inf')
    while not stop.is_set():
        try:
            active = process_one(repo)
            if settings.discord_webhook:
                dispatch_one(repo, settings.discord_webhook)
            if time.monotonic() - last_cleanup >= 60:
                repo.cleanup()
                last_cleanup = time.monotonic()
        except Exception:  # noqa: BLE001 - never leak credentials in third-party errors
            # A DB outage leaves the durable reservation for lease recovery.
            LOGGER.error('Worker operation failed; retrying after delay')
            stop.wait(5)
        else:
            if not active:
                stop.wait(1)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        settings = Settings.from_env()
        repo = Repository(settings.database_url, Cipher(settings.secret_key),
                          min_interval_seconds=settings.min_interval_seconds,
                          lease_seconds=settings.lease_seconds)
        if not repo.health():
            raise ValueError('Service database is not ready')
    except Exception:  # noqa: BLE001 - never leak credentials in third-party errors
        LOGGER.error('Worker configuration or database is not ready')
        return 1
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    run_loop(repo, settings, stop)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
