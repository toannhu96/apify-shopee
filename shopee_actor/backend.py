"""Bounded, authenticated service client. No user-controlled destinations."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE = 4 * 1024 * 1024
ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


class BackendError(RuntimeError):
    """Safe service error that never embeds URLs, headers or response bodies."""


@dataclass(frozen=True)
class Settings:
    url: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        url = env.get("SH_BACKEND_URL", "").rstrip("/")
        try:
            parsed = urlsplit(url)
            _ = parsed.port
        except ValueError:
            raise ValueError("Invalid backend origin") from None
        local = (
            env.get("SH_BACKEND_ALLOW_HTTP") == "1"
            and env.get("APIFY_IS_AT_HOME") != "1"
            and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        )
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(ord(c) < 33 for c in url)
            or not (parsed.scheme == "https" or (local and parsed.scheme == "http"))
        ):
            raise ValueError("A fixed HTTPS backend origin is required")
        token = env.get("SH_BACKEND_TOKEN", "")
        if env.get("SH_BACKEND_TOKEN_FILE"):
            if token:
                raise ValueError("Configure one backend token source")
            try:
                with Path(env["SH_BACKEND_TOKEN_FILE"]).open("r") as stream:
                    token = stream.read(4097).strip()
            except (OSError, UnicodeError):
                raise ValueError("Backend token file is unreadable") from None
        if not 32 <= len(token) <= 4096 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ValueError("A valid backend authentication token is required")
        return cls(url, token)


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise BackendError("Invalid backend operation identifier")
    return value


def _retry_delay(response, attempt):
    try:
        delay = float(response.headers.get("retry-after", 2**attempt))
        return max(0, delay) if math.isfinite(delay) else 120
    except ValueError:
        return 2**attempt


class BackendClient:
    def __init__(self, settings: Settings, *, transport=None, sleep=asyncio.sleep):
        self._client = httpx.AsyncClient(
            base_url=settings.url,
            headers={"Authorization": "Bearer " + settings.token},
            follow_redirects=False,
            timeout=httpx.Timeout(20, connect=5),
            trust_env=False,
            transport=transport,
        )
        self._sleep = sleep
        self._control = None
        self._deadline = None

    def set_control(self, control):
        self._control = control

    def _stopped(self):
        return (self._control is not None and self._control.reason is not None) or (
            self._deadline is not None and datetime.now(UTC) >= self._deadline
        )

    async def _pause(self, seconds):
        while seconds > 0:
            if self._stopped():
                raise BackendError("Backend wait interrupted by shutdown or deadline")
            part = min(1, seconds)
            await self._sleep(part)
            seconds -= part
        if self._stopped():
            raise BackendError("Backend wait interrupted by shutdown or deadline")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self._client.aclose()

    async def _request(self, method, path, *, payload=None, missing_ok=False):
        failures, waited = 0, 0
        for attempt in range(10):
            try:
                async with self._client.stream(method, path, json=payload) as response:
                    if response.status_code == 404 and missing_ok:
                        return {}
                    if response.status_code == 429:
                        delay = _retry_delay(response, attempt)
                        if waited + delay > 120:
                            raise BackendError(
                                "Backend rate limit exceeded the bounded wait"
                            )
                        waited += delay
                        await self._pause(delay)
                        continue
                    if response.status_code >= 500:
                        failures += 1
                        if failures < 3:
                            await self._pause(2 ** (failures - 1))
                            continue
                        raise BackendError("Backend unavailable after bounded retries")
                    if not 200 <= response.status_code < 300:
                        raise BackendError("Backend rejected the operation")
                    parts, size = [], 0
                    async for part in response.aiter_bytes():
                        size += len(part)
                        if size > MAX_RESPONSE:
                            raise BackendError("Backend response exceeds its limit")
                        parts.append(part)
                    value = json.loads(b"".join(parts))
                    if not isinstance(value, dict):
                        raise BackendError("Invalid backend response")
                    return value
            except httpx.TransportError:
                failures += 1
                if failures < 3:
                    await self._pause(2 ** (failures - 1))
                    continue
                raise BackendError(
                    "Backend connection failed after bounded retries"
                ) from None
            except (ValueError, UnicodeError, RecursionError):
                raise BackendError("Invalid backend response") from None
        raise BackendError("Backend operation failed")

    async def submit(self, run_id, ids, deadline):
        _identifier(run_id)
        self._deadline = datetime.fromisoformat(deadline)
        return await self._request(
            "POST",
            "/v1/runs",
            payload={"runId": run_id, "productIds": ids, "deadlineAt": deadline},
        )

    async def poll(self, run_id, cursor):
        run_id = _identifier(run_id)
        if type(cursor) is not int or cursor < 0:
            raise BackendError("Invalid result cursor")
        return await self._request(
            "GET", f"/v1/runs/{run_id}?cursor={cursor}&limit=100"
        )

    async def cancel(self, run_id, reason):
        allowed = {
            "cancelled",
            "budget_exhausted",
            "deadline_exceeded",
            "billing_uncertain",
        }
        reason = reason if reason in allowed else "cancelled"
        deadline, control = self._deadline, self._control
        self._deadline, self._control = None, None
        try:
            return await self._request(
                "POST",
                f"/v1/runs/{_identifier(run_id)}/cancel",
                payload={"reason": reason},
                missing_ok=True,
            )
        finally:
            self._deadline, self._control = deadline, control

    async def deliveries(self, run_id):
        value = await self._request("GET", f"/v1/runs/{_identifier(run_id)}/deliveries")
        rows = value.get("deliveries")
        if (
            not isinstance(rows, list)
            or len(rows) > 1000
            or any(
                not isinstance(row, dict) or not isinstance(row.get("itemId"), str)
                for row in rows
            )
        ):
            raise BackendError("Invalid delivery ledger")
        if len({r["itemId"] for r in rows}) != len(rows):
            raise BackendError("Duplicate delivery ledger entry")
        return {row["itemId"]: row for row in rows}

    async def record(self, run_id, item_id, result_id, phase):
        return await self._request(
            "POST",
            f"/v1/runs/{_identifier(run_id)}/deliveries/{_identifier(item_id)}",
            payload={"phase": phase, "resultId": result_id},
        )
