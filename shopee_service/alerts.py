"""One-at-a-time delivery of sanitized durable Discord incident alerts."""
from __future__ import annotations

import logging
import math

from .config import validate_webhook


class _TransportLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX logs full URLs at INFO. HTTPCore and HTTP/2 libraries can log
        # request paths/headers independently at DEBUG, including bare tokens.
        # Transport diagnostics are intentionally unavailable in this service.
        return not any(record.name == prefix or record.name.startswith(prefix + '.')
                       for prefix in ('httpx', 'httpcore', 'h2', 'hpack'))


def _install_log_filter() -> None:
    transport_filter = _TransportLogFilter()
    root = logging.getLogger()
    loggers = [root, *(logger for logger in list(logging.Logger.manager.loggerDict.values())
                       if isinstance(logger, logging.Logger))]
    for logger in loggers:
        if (any(logger.name == prefix or logger.name.startswith(prefix + '.')
                for prefix in ('httpx', 'httpcore', 'h2', 'hpack'))
                and not any(isinstance(value, _TransportLogFilter) for value in logger.filters)):
            logger.addFilter(transport_filter)
        # Parent logger filters do not apply to descendants. Handler filtering
        # also protects transport loggers created lazily after client creation.
        for handler in logger.handlers:
            if not any(isinstance(value, _TransportLogFilter) for value in handler.filters):
                handler.addFilter(transport_filter)


def _retry_after(response, attempts: int) -> float:
    default = min(3600, 30 * 2 ** min(max(attempts, 0), 7))
    try:
        payload = response.json()
        raw = payload.get('retry_after') if isinstance(payload, dict) else None
        if raw is None:
            raw = response.headers.get('Retry-After', default)
        value = float(raw)
        if not math.isfinite(value):
            return default
        return max(1, min(3600, value))
    except (ValueError, TypeError, AttributeError):
        return default


def _send(client, webhook: str, alert: dict) -> tuple[str | None, float | None]:
    _install_log_filter()  # Client construction may have imported new transport loggers.
    try:
        payload = {**alert['payload'], 'allowed_mentions': {'parse': []}}
        response = client.post(webhook, params={'wait': 'true'}, json=payload, timeout=10,
                               follow_redirects=False)
        if 200 <= response.status_code < 300:
            return None, None
        if response.status_code == 429:
            return 'rate_limited', _retry_after(response, alert['attempts'])
        if 400 <= response.status_code < 500:
            return 'delivery_rejected', None
    except Exception:  # noqa: BLE001 - transport exception text can contain the secret webhook URL
        # HTTP exceptions may embed the webhook credential in their URL.
        return 'delivery_failed', min(3600, 30 * 2 ** min(max(alert['attempts'], 0), 7))
    return 'delivery_failed', min(3600, 30 * 2 ** min(max(alert['attempts'], 0), 7))


def dispatch_one(repo, webhook: str | None, *, client=None) -> bool:
    """Deliver one claimed event; failed events remain durable for a later retry."""
    if not webhook:
        return False
    validate_webhook(webhook)
    _install_log_filter()
    alert = repo.claim_alert()
    if alert is None:
        return False
    if client is None:
        import httpx
        with httpx.Client(trust_env=False) as transport:
            error, retry_after = _send(transport, webhook, alert)
    else:
        error, retry_after = _send(client, webhook, alert)
    if error is None:
        repo.finish_alert(alert['id'], alert['lease_token'])
    else:
        repo.finish_alert(alert['id'], alert['lease_token'], error=error, retry_after=retry_after)
    return True
