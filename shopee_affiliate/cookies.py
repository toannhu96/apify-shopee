"""Parse user-supplied Shopee cookies without logging credential values."""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urlparse

BASE = "https://affiliate.shopee.vn"
HOSTS = {"shopee.vn", "affiliate.shopee.vn"}
COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _pair(name: Any, value: Any) -> dict[str, str]:
    if not isinstance(name, str) or not COOKIE_NAME.fullmatch(name):
        raise ValueError("Invalid cookie name")
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or c == ";" for c in value):
        raise ValueError("Invalid cookie value")
    return {"name": name, "value": value}


def _json_cookie(cookie: Any) -> dict[str, Any]:
    if not isinstance(cookie, dict):
        raise ValueError("Cookie entries must be objects")  # noqa: TRY004 - malformed external data
    pair = _pair(cookie.get("name"), cookie.get("value"))
    if "hostOnly" in cookie and not isinstance(cookie["hostOnly"], bool):
        raise ValueError("Invalid cookie hostOnly flag")
    if "url" in cookie:
        parsed = urlparse(str(cookie["url"]))
        if (parsed.scheme != "https" or parsed.netloc not in HOSTS or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or "domain" in cookie
                or cookie.get("hostOnly") is False):
            raise ValueError("Cookie URL must be an HTTPS Shopee VN origin")
        scope = {"url": f"https://{parsed.netloc}"}
    else:
        domain, path = cookie.get("domain"), cookie.get("path", "/")
        if not isinstance(domain, str) or domain.removeprefix(".") not in HOSTS:
            raise ValueError("Cookie domain must be Shopee VN or its affiliate host")
        if "hostOnly" in cookie:
            domain = ("" if cookie["hostOnly"] else ".") + domain.removeprefix(".")
        if not isinstance(path, str) or not path.startswith("/") or any(ord(c) < 32 for c in path):
            raise ValueError("Invalid cookie path")
        scope = {"domain": domain, "path": path}
    options: dict[str, Any] = {"secure": True}
    if "httpOnly" in cookie:
        if not isinstance(cookie["httpOnly"], bool):
            raise ValueError("Invalid cookie httpOnly flag")
        options = {**options, "httpOnly": cookie["httpOnly"]}
    if "sameSite" in cookie:
        if cookie["sameSite"] not in ("Strict", "Lax", "None"):
            raise ValueError("Invalid cookie sameSite setting")
        options = {**options, "sameSite": cookie["sameSite"]}
    if "expires" in cookie:
        expires = cookie["expires"]
        try:
            valid_expiry = (not isinstance(expires, bool) and isinstance(expires, (int, float))
                            and math.isfinite(expires) and (expires >= 0 or expires == -1))
        except OverflowError:
            valid_expiry = False
        if not valid_expiry:
            raise ValueError("Invalid cookie expiration")
        options = {**options, "expires": expires}
    return {**pair, **scope, **options}


def parse_cookies(source: str) -> list[dict[str, Any]]:
    """Accept a Cookie header, JSON cookie array, or Playwright storage-state JSON.

    Raw headers become host-only cookies for affiliate.shopee.vn. JSON cookies
    retain their Shopee VN scope. Storage values and unknown fields are ignored.
    """
    if not isinstance(source, str) or len(source) > 1_048_576 or not source.strip():
        raise ValueError("Cookie input must be nonempty and at most 1 MiB")
    text = source.strip()
    if ("\r" in text or "\n" in text) and not text.startswith(("[", "{")):
        raise ValueError("Cookie header must be one line")
    if text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise ValueError("Invalid cookie JSON") from None
        entries = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(entries, list) or not entries:
            raise ValueError("Cookie JSON must contain a nonempty cookie list")
        cookies = [_json_cookie(entry) for entry in entries]
    else:
        parts = [part.strip().partition("=") for part in text.split(";")]
        if any(not separator for _, separator, _ in parts):
            raise ValueError("Cookie header must contain name=value pairs")
        cookies = [{**_pair(name, value), "url": BASE, "secure": True} for name, _, value in parts]
    identities = [(c["name"], c.get("url", c.get("domain")), c.get("path", "/")) for c in cookies]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate cookie names for the same scope")
    return cookies
