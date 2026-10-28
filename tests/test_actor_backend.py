import asyncio
import json

import httpx
import pytest

from shopee_actor.backend import BackendClient, BackendError, Settings


def test_config_requires_fixed_safe_https_origin_and_secret():
    s = Settings.from_env(
        {"SH_BACKEND_URL": "https://backend.example", "SH_BACKEND_TOKEN": "x" * 32}
    )
    assert s.url == "https://backend.example"
    assert "x" * 32 not in repr(s)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://x@y.com",
        "https://example.com/path",
        "https://example.com?token=x",
        "https://example.com#x",
        "file:///tmp/a",
        "https://",
    ],
)
def test_bad_backend_origin_rejected(url):
    with pytest.raises(ValueError):
        Settings.from_env({"SH_BACKEND_URL": url, "SH_BACKEND_TOKEN": "x" * 32})


def test_local_http_opt_in_never_works_on_apify():
    env = {
        "SH_BACKEND_URL": "http://127.0.0.1:8000",
        "SH_BACKEND_TOKEN": "x" * 32,
        "SH_BACKEND_ALLOW_HTTP": "1",
    }
    assert Settings.from_env(env).url.startswith("http:")
    with pytest.raises(ValueError):
        Settings.from_env({**env, "APIFY_IS_AT_HOME": "1"})


def client(handler):
    return BackendClient(
        Settings("https://backend.example", "x" * 32),
        transport=httpx.MockTransport(handler),
        sleep=lambda _: asyncio.sleep(0),
    )


def test_submit_poll_and_ledger_contract():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/deliveries"):
            return httpx.Response(
                200,
                json={
                    "deliveries": [
                        {"itemId": "1", "phase": "charged", "resultId": "run1:1"}
                    ]
                },
            )
        return httpx.Response(200, json={"results": [], "done": True, "nextCursor": 0})

    async def go():
        async with client(handler) as c:
            await c.submit("run1", ["1"], "2026-10-28T00:00:00Z")
            await c.poll("run1", 0)
            ledger = await c.deliveries("run1")
            await c.record("run1", "1", "run1:1", "charged")
            assert ledger["1"]["phase"] == "charged"

    asyncio.run(go())
    assert json.loads(seen[0].content)["productIds"] == ["1"]
    assert seen[0].headers["authorization"] == "Bearer " + "x" * 32
    assert str(seen[1].url).endswith("?cursor=0&limit=100")


def test_transient_retry_bounded_and_no_secret_error():
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(503, text="cookie=secret")

    async def go():
        async with client(handler) as c:
            with pytest.raises(BackendError) as err:
                await c.poll("run1", 0)
            assert "secret" not in str(err.value)

    asyncio.run(go())
    assert len(attempts) == 3


def test_cancel_unknown_run_is_idempotent():
    async def go():
        async with client(lambda _: httpx.Response(404)) as c:
            await c.cancel("run1", "aborted")

    asyncio.run(go())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://evil.example"}),
        httpx.Response(401, text="secret"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)),
    ],
)
def test_unsafe_response_rejected(response):
    async def go():
        async with client(lambda _: response) as c:
            with pytest.raises(BackendError):
                await c.poll("run1", 0)

    asyncio.run(go())


def test_path_identifiers_cannot_escape_route():
    async def go():
        async with client(lambda _: pytest.fail("network")) as c:
            with pytest.raises(BackendError):
                await c.poll("../secret", 0)

    asyncio.run(go())


def test_rate_limit_honors_window_then_succeeds():
    seen = []
    waits = []

    def handler(request):
        seen.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "61"})
            if len(seen) == 1
            else httpx.Response(200, json={"done": True})
        )

    async def sleep(seconds):
        waits.append(seconds)

    async def go():
        async with BackendClient(
            Settings("https://backend.example", "x" * 32),
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        ) as c:
            assert await c.poll("run1", 0) == {"done": True}

    asyncio.run(go())
    assert sum(waits) == 61
    assert len(seen) == 2


def test_rate_limit_stops_on_migration():
    from shopee_actor.engine import Control

    c = Control()

    async def sleep(_):
        c.stop("migration")

    async def go():
        async with BackendClient(
            Settings("https://backend.example", "x" * 32),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": "61"})
            ),
            sleep=sleep,
        ) as b:
            b.set_control(c)
            with pytest.raises(BackendError):
                await b.poll("run1", 0)

    asyncio.run(go())


def test_cancel_normalizes_internal_reason():
    seen = []

    async def go():
        async with client(
            lambda r: seen.append(r) or httpx.Response(200, json={})
        ) as c:
            await c.cancel("run1", "aborted")

    asyncio.run(go())
    assert json.loads(seen[0].content) == {"reason": "cancelled"}
