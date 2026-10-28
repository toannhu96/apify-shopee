"""Validated service configuration with file-based Docker secret support."""
from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class ConfigurationError(ValueError):
    """Safe-to-display configuration failure without secret values."""


def _value(environ: Mapping[str, str], name: str, *, required: bool = True) -> str:
    try:
        if file_name := environ.get(name + '_FILE'):
            with Path(file_name).open('rb') as source:
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            value = raw.decode('utf-8').rstrip('\r\n')
        else:
            value = environ.get(name, '')
        if not isinstance(value, str) or len(value) > 16384 or (required and not value):
            raise ValueError()
        if any(ord(char) < 32 for char in value):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise ConfigurationError(f'{name} is missing or invalid') from None


def validate_webhook(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname in {'discord.com', 'discordapp.com'}
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment
                 and re.fullmatch(r'/api(?:/v\d+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+', parsed.path))
        if not valid:
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigurationError('SH_DISCORD_WEBHOOK is invalid') from None
    return value


def _number(environ: Mapping[str, str], name: str, default: float, minimum: float,
            maximum: float) -> float:
    try:
        value = float(environ.get(name, default))
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ConfigurationError(f'{name} is invalid') from None


def _integer(environ, name, default, minimum, maximum):
    value = str(environ.get(name, default))
    if not value.isascii() or not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ConfigurationError(f'{name} is invalid')
    return int(value)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_token: str = field(repr=False)
    secret_key: str = field(repr=False)
    discord_webhook: str | None = field(default=None, repr=False)
    min_interval_seconds: float = 6
    lease_seconds: float = 90
    api_rate_limit: int = 600

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        database_url = _value(source, 'SH_DATABASE_URL')
        api_token = _value(source, 'SH_API_TOKEN')
        secret_key = _value(source, 'SH_SECRET_KEY')
        try:
            if urlsplit(database_url).scheme not in {'postgresql', 'postgres'}:
                raise ValueError()
        except ValueError:
            raise ConfigurationError('SH_DATABASE_URL is invalid') from None
        if len(api_token) < 32 or not api_token.isascii() or any(c.isspace() for c in api_token):
            raise ConfigurationError('SH_API_TOKEN must contain at least 32 non-whitespace ASCII characters')
        try:
            Fernet(secret_key.encode('ascii'))
        except (ValueError, TypeError, UnicodeError):
            raise ConfigurationError('SH_SECRET_KEY is invalid') from None
        return cls(database_url, api_token, secret_key,
                   validate_webhook(_value(source, 'SH_DISCORD_WEBHOOK', required=False)),
                   _number(source, 'SH_MIN_INTERVAL_SECONDS', 6, 6, 3600),
                   _number(source, 'SH_LEASE_SECONDS', 90, 60, 3600),
                   _integer(source, 'SH_API_RATE_LIMIT', 600, 60, 60000))
