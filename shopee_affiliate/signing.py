"""Opt-in adapter for a user-selected experimental offline Node signer.

The harness executes local code. This module passes only a validated product URL
and a null body, never cookies or the caller's environment. A generated signature
does not establish that Shopee accepts the request.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path

ENDPOINT_PATTERN = re.compile(
    r"https://affiliate\.shopee\.vn/api/v3/offer/product\?item_id=[1-9][0-9]{0,19}"
)
SIGNATURE_KEYS = frozenset({"x-sap-ri", "x-sap-sec"})
MAX_SIGNER_BYTES = 32768


class SigningError(RuntimeError):
    """Safe diagnostic with no subprocess output, headers, or session values."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _validate_output(output: str) -> dict[str, str]:
    if len(output) > MAX_SIGNER_BYTES:
        raise SigningError("Experimental signer output exceeds the size limit")
    try:
        headers = json.loads(output, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise SigningError("Experimental signer returned invalid JSON") from None
    if not isinstance(headers, dict) or set(headers) != SIGNATURE_KEYS:
        raise SigningError("Experimental signer returned unexpected header names")
    for name, value in headers.items():
        minimum = 64 if name == "x-sap-sec" else 16
        maximum = 16384 if name == "x-sap-sec" else 1024
        if (not isinstance(value, str) or not minimum <= len(value) <= maximum
                or any(ord(char) < 33 or ord(char) > 126 for char in value)):
            raise SigningError("Experimental signer returned invalid or fallback headers")
    return headers


def _read_stdout(stream, deadline: float) -> bytes:
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise SigningError("Experimental signer timed out")
            chunk = os.read(stream.fileno(), min(4096, MAX_SIGNER_BYTES + 1 - len(output)))
            if not chunk:
                return bytes(output)
            output.extend(chunk)
            if len(output) > MAX_SIGNER_BYTES:
                raise SigningError("Experimental signer output exceeds the size limit")


def _run_signer(command: list[str], *, input: str, timeout: float) -> str:
    """Drain at most 32 KiB from stdout and discard stderr without buffering it."""
    deadline = time.monotonic() + timeout
    process = None
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        )
        process.stdin.write(input.encode("utf-8"))
        process.stdin.close()
        output = _read_stdout(process.stdout, deadline)
        status = process.wait(timeout=max(0, deadline - time.monotonic()))
        if status != 0:
            raise SigningError("Experimental signer execution failed")
        return output.decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SigningError("Experimental signer execution failed or timed out") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdin.close()
            process.stdout.close()


def sign_headers(harness: str | Path, url: str) -> dict[str, str]:
    """Run one offline signer process for the exact validated product URL."""
    if not isinstance(url, str) or not ENDPOINT_PATTERN.fullmatch(url):
        raise SigningError("Experimental signer requires the exact product endpoint")
    try:
        path = Path(harness).expanduser().resolve(strict=True)
        if not path.is_file():
            raise OSError
    except (OSError, ValueError, TypeError, RuntimeError):
        raise SigningError("Experimental signer path must be an existing local file") from None
    node = shutil.which("node")
    if not node:
        raise SigningError("Node.js is required for the experimental signer")
    try:
        output = _run_signer(
            [node, "--max-old-space-size=192", str(path), "--headers"],
            input=json.dumps({"url": url, "body": None}), timeout=15,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise SigningError("Experimental signer execution failed or timed out") from None
    return _validate_output(output)
