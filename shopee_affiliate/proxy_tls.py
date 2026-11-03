"""Validate an operator CA bundle used only for the HTTPS proxy TLS layer."""
from __future__ import annotations

import os
import re
import ssl
import stat

MAX_PROXY_CA_BYTES = 256 * 1024
_CERTIFICATES = re.compile(
    r'\s*(?:-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*)+'
)


def proxy_ca_file() -> str | None:
    """Read SH_PROXY_CA_FILE locally; reject invalid files without exposing paths.

    Call only when the request has an explicit HTTPS proxy. This does not change
    global certificate configuration or the destination server's trust store.
    """
    configured = os.environ.get('SH_PROXY_CA_FILE')
    if configured is None:
        return None
    try:
        if (not configured or len(configured) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in configured)):
            raise ValueError
        path = os.path.abspath(configured)
        # NONBLOCK prevents an operator typo pointing at a FIFO from hanging.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PROXY_CA_BYTES
                    or not info.st_mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)):
                raise ValueError
            raw = stream.read(MAX_PROXY_CA_BYTES + 1)
        if not 0 < len(raw) <= MAX_PROXY_CA_BYTES:
            raise ValueError
        pem = raw.decode('ascii')
        if not _CERTIFICATES.fullmatch(pem):
            raise ValueError
        # Parse the complete bundle with OpenSSL without making a connection.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
        return path
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Invalid operator HTTPS proxy CA configuration') from None
