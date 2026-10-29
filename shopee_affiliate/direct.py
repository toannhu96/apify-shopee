"""Browser-free, single-request Shopee Affiliate product client.

Uses exported cookies by default. An explicit --experimental-signer local Node
harness can supply signing headers, whose server acceptance remains unverified.
Run: python -m shopee_affiliate.direct ITEM_ID --cookie-file /private/cookies.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import sys
import time
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .cookies import parse_cookies
from .parse import normalize_detail
from .signing import SigningError

ENDPOINT = "https://affiliate.shopee.vn/api/v3/offer/product"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_COOKIE_BYTES = 1024 * 1024
ALLOWED_HEADERS = frozenset({"x-sap-ri", "x-sap-sec", "af-ac-enc-sz-token",
                            "af-ac-enc-dat", "x-sz-sdk-version", "user-agent"})


class DirectError(RuntimeError):
    """Safe-to-display error without request secrets or response contents."""
    category = 'invalid_response'

    def __init__(self, message='', *, http_status=None, api_code=None, response_received=False):
        super().__init__(message)
        self.response_received = response_received is True
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.api_code = api_code if type(api_code) is int and abs(api_code) < 10**10 else None


class DirectBlockedError(DirectError):
    """Server rejected authentication or traffic; do not retry automatically."""
    category = 'verification'


class DirectAuthenticationError(DirectBlockedError):
    category = 'authentication'


class DirectVerificationError(DirectBlockedError):
    category = 'verification'


class DirectRateLimitError(DirectBlockedError):
    category = 'rate_limit'


class DirectTransientError(DirectError):
    category = 'transient'


class DirectUnavailableError(DirectError):
    category = 'unavailable'


class DirectInputError(DirectError):
    category = 'input'


class DirectSigningError(DirectError):
    category = 'signing'


class NoApplicableCookiesError(ValueError):
    """An otherwise valid export contains no usable endpoint cookies."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _item_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Product ID must be a positive decimal integer")  # noqa: TRY004 - external input
    result = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,19}", result):
        raise ValueError("Product ID must be a positive decimal integer of at most 20 digits")
    return result


def cookie_header(source: str) -> str:
    """Select unexpired cookies applicable to the exact fixed HTTPS endpoint.

    URL cookies and domains without a leading dot are host-only. Path ordering
    preserves the browser rule that longer matching paths appear first.
    """
    target = urlsplit(ENDPOINT)
    now = time.time()
    applicable = []
    for cookie in parse_cookies(source):
        domain = cookie.get("domain")
        if domain:
            host = domain.removeprefix(".")
            host_matches = target.hostname == host or (
                domain.startswith(".") and target.hostname.endswith("." + host))
        else:
            host_matches = urlsplit(cookie["url"]).hostname == target.hostname
        path = cookie.get("path", "/")
        path_matches = target.path == path or (target.path.startswith(path) and (
            path.endswith("/") or target.path[len(path):].startswith("/")))
        expires = cookie.get("expires", -1)
        if host_matches and path_matches and (expires == -1 or expires > now):
            applicable.append(cookie)
    if not applicable:
        raise NoApplicableCookiesError("No unexpired cookies apply to the affiliate product endpoint")
    ordered = sorted(applicable, key=lambda cookie: -len(cookie.get("path", "/")))
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in ordered)


def _headers(source: str, optional: dict[str, str] | None) -> dict[str, str]:
    if optional is not None and not isinstance(optional, dict):
        raise ValueError("Optional headers must be an object")
    extra = {}
    for name, value in (optional or {}).items():
        if (not isinstance(name, str) or name.lower() not in ALLOWED_HEADERS
                or not isinstance(value, str) or len(value) > 16384
                or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            raise ValueError("Invalid optional request header")
        if name.lower() in extra:
            raise ValueError("Duplicate optional request header")
        extra[name.lower()] = value
    return {"Accept": "application/json", "Affiliate-Program-Type": "1",
            "Referer": "https://affiliate.shopee.vn/offer/product_offer",
            "Cookie": cookie_header(source), **extra}


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON key")
        result[name] = value
    return result


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("JSON number exceeds supported range")
    return result


def _validate(raw: bytes, item_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise DirectError("Response is not strict UTF-8 JSON") from None
    if not isinstance(payload, dict):
        raise DirectError("Unexpected product response structure")
    code = payload.get("error", payload.get("code"))
    if code in (90309999, "90309999"):
        raise DirectVerificationError("Shopee rejected the request (API 90309999)", api_code=90309999)
    if payload.get("is_login") is False:
        raise DirectAuthenticationError("Shopee requires login or traffic verification", api_code=code)
    if payload.get("redirect_to_error_page") is True:
        raise DirectVerificationError("Shopee requires login or traffic verification", api_code=code)
    if "error" in payload or type(code) is not int or code != 0:
        suffix = f" (API {code})" if type(code) is int and abs(code) < 10**10 else ""
        raise DirectError("Shopee API request failed" + suffix, api_code=code)
    data = payload.get("data")
    if not isinstance(data, dict) or str(data.get("item_id")) != item_id:
        raise DirectError("Product response does not match requested item")
    card = data.get("batch_item_for_item_card_full")
    if not isinstance(card, dict) or not card:
        raise DirectError("Product response is missing product data")
    if "itemid" in card and str(card["itemid"]) != item_id:
        raise DirectError("Product card does not match requested item")
    try:
        product = normalize_detail(data)
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, OverflowError):
        raise DirectError("Product data could not be normalized") from None
    return {"raw": payload, "product": product}


def fetch_product(cookie_header: str, item_id: str | int,
                  optional_headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Fetch one item, returning raw and normalized data; never follow redirects.

    optional_headers accepts only observed Shopee signing header names and a
    User-Agent. Supplied signatures may be stale; no signer is implemented here.
    """
    identifier = _item_id(item_id)
    headers = _headers(cookie_header, optional_headers)
    request = Request(ENDPOINT + "?item_id=" + identifier, headers=headers)
    opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=30) as response:
            if response.status != 200:
                raise DirectError(f"Unexpected HTTP status {response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        error_class = DirectBlockedError if status in (401, 403, 429) else DirectError
        raise error_class(f"Request rejected (HTTP {status})") from None
    except (URLError, OSError, ValueError, HTTPException):
        raise DirectError("Network request failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DirectError("Response exceeds the size limit")
    return _validate(raw, identifier)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("item_id")
    parser.add_argument("--experimental-signer", type=Path,
                        help="Execute this local Node signer; server acceptance is unverified")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--cookie-file", type=Path)
    group.add_argument("--cookie-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cookie_file:
            with args.cookie_file.open(encoding="utf-8") as stream:
                source = stream.read(MAX_COOKIE_BYTES + 1)
        elif args.cookie_stdin:
            source = sys.stdin.read(MAX_COOKIE_BYTES + 1)
        else:
            source = os.environ.get("SHOPEE_COOKIES", "")
        signatures = None
        if args.experimental_signer:
            from .signing import sign_headers

            identifier = _item_id(args.item_id)
            cookie_header(source)
            signatures = sign_headers(args.experimental_signer, ENDPOINT + "?item_id=" + identifier)
        result = fetch_product(source, args.item_id, signatures)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except DirectBlockedError as error:
        print(str(error), file=sys.stderr)
        return 2
    except SigningError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (DirectError, ValueError) as error:
        print(str(error) if isinstance(error, DirectError) else "Cookie input or product ID is invalid", file=sys.stderr)
        return 1
    except OSError:
        print("Could not read cookie input or write output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
