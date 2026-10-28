import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from shopee_actor.runtime import ActorPort, billing_enabled, identity


def config(**kwargs):
    return SimpleNamespace(
        is_at_home=False,
        test_pay_per_event=False,
        actor_pricing_info=None,
        actor_run_id=None,
        started_at=None,
        **kwargs,
    )


def test_local_billing_disabled_and_cloud_missing_pricing_rejected():
    assert billing_enabled(config(), {}) is False
    c = config()
    c.is_at_home = True
    with pytest.raises(ValueError, match="product-result"):
        billing_enabled(c, {})
    assert not billing_enabled(c, {"SH_ALLOW_UNBILLED_TEST": "1"})


def test_cloud_pricing_validated():
    event = SimpleNamespace(
        event_price_usd=0.01, event_tiered_pricing_usd=None, is_one_time_event=False
    )
    c = config()
    c.is_at_home = True
    c.actor_pricing_info = SimpleNamespace(
        pricing_model="PAY_PER_EVENT",
        pricing_per_event=SimpleNamespace(
            actor_charge_events={"product-result": event}
        ),
    )
    assert billing_enabled(c, {}) is True
    event.is_one_time_event = True
    with pytest.raises(ValueError):
        billing_enabled(c, {})


def test_identity_resumes_saved_local_run_and_deadline():
    state = {"runId": "local-one", "deadlineAt": "2026-10-28T02:00:00Z"}
    run_id, started = identity(config(), state)
    assert run_id == "local-one"
    assert started == datetime(2026, 10, 28, tzinfo=UTC)


def test_identity_uses_platform_configuration():
    c = config()
    c.actor_run_id = "abc123"
    c.started_at = datetime(2026, 10, 28, tzinfo=UTC)
    assert identity(c, None) == ("abc123", c.started_at)


def test_port_only_scans_result_ids_and_exposes_actual_charges():
    values = {}

    class Dataset:
        async def iterate_items(self, **kwargs):
            assert kwargs == {"fields": ["resultId"], "limit": 1001}
            yield {"resultId": "r:1"}

    class Actor:
        async def open_dataset(self):
            return Dataset()

        async def get_value(self, key):
            return values.get(key)

        async def set_value(self, key, value):
            values[key] = value

        async def push_data(self, row):
            values["row"] = row

        async def charge(self, event_name, **kwargs):
            assert event_name == "product-result"
            assert kwargs == {"idempotency_key": "r:1"}
            return SimpleNamespace(charged_count=1)

        def get_charging_manager(self):
            return SimpleNamespace(
                calculate_max_event_charge_count_within_limit=lambda _: None
            )

        async def set_status_message(self, msg, **kwargs):
            values["status"] = msg

    async def go():
        port = ActorPort(Actor(), charged=True)
        assert await port.existing_result_ids() == {"r:1"}
        assert port.remaining_budget() == 1000
        await port.save_state({"cursor": 1})
        assert await port.load_state() == {"cursor": 1}
        await port.push({"x": 1})
        assert await port.charge("r:1")
        await port.summarize(
            {"status": "partial", "deliveredCount": 1, "requestedCount": 2}
        )
        assert values["SUMMARY"]["billingMode"] == "pay-per-event"
        free = ActorPort(Actor(), charged=False)
        assert await free.charge("r:1")

    asyncio.run(go())


def test_cloud_dataset_lost_ack_is_not_retried_by_api_client(monkeypatch):
    import impit
    from apify import Configuration
    from apify_client.http_clients import ImpitHttpClientAsync

    attempts = []

    async def committed_then_disconnect(self, **kwargs):
        attempts.append(kwargs)
        raise impit.HTTPError("Acknowledgement lost after append")

    monkeypatch.setattr(ImpitHttpClientAsync, "send_request", committed_then_disconnect)
    actor = SimpleNamespace(
        configuration=Configuration(
            is_at_home=True, token="test-token", default_dataset_id="dataset1"
        )
    )

    async def go():
        port = ActorPort(actor, charged=True)
        try:
            with pytest.raises(impit.HTTPError):
                await port.push({"resultId": "r:1"})
        finally:
            await port.close()

    asyncio.run(go())
    assert len(attempts) == 1
    assert attempts[0]["method"] == "POST"
    assert "/datasets/dataset1/items" in attempts[0]["url"]


@pytest.mark.parametrize(
    ("status", "reason", "expected_failure", "expected_reboot"),
    [
        ("succeeded", "completed", False, False),
        ("partial", "completed", True, False),
        ("failed", "budget_exhausted", False, False),
        ("partial", "migration", False, True),
        ("partial", "aborted", False, False),
    ],
)
def test_runtime_registers_events_and_handles_outcome(
    monkeypatch, status, reason, expected_failure, expected_reboot
):
    from apify import Event

    from shopee_actor import runtime

    calls = []
    c = config()
    c.is_at_home = True

    class Actor:
        configuration = c

        async def get_input(self):
            return {"productIds": ["1"]}

        async def get_value(self, _):
            return None

        def on(self, event, callback):
            calls.append(("on", event, callback))

        def off(self, event, callback):
            calls.append(("off", event, callback))

        async def fail(self, **kwargs):
            calls.append(("fail", kwargs))

        async def reboot(self):
            calls.append(("reboot",))

    class Backend:
        def __init__(self, _):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def set_control(self, control):
            pass

    async def execute(*args, **kwargs):
        for name, event, callback in [x for x in calls if x[0] == "on"]:
            if event == Event.MIGRATING and reason == "migration":
                await callback()
            if event == Event.ABORTING and reason == "aborted":
                await callback()
        return {"status": status, "reason": reason}

    monkeypatch.setattr(runtime, "execute", execute)
    env = {
        "SH_BACKEND_URL": "https://backend.example",
        "SH_BACKEND_TOKEN": "x" * 32,
        "SH_ALLOW_UNBILLED_TEST": "1",
    }
    asyncio.run(runtime.run_actor(Actor(), env=env, backend_factory=Backend))
    assert len([x for x in calls if x[0] == "off"]) == 2
    assert bool([x for x in calls if x[0] == "fail"]) == expected_failure
    assert bool([x for x in calls if x[0] == "reboot"]) == expected_reboot


def test_main_sanitizes_unhandled_exception(monkeypatch):
    from shopee_actor import runtime

    messages = []

    class Actor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def fail(self, **kwargs):
            messages.append(kwargs)

    async def run_actor():
        raise RuntimeError("cookie=secret")

    monkeypatch.setattr(runtime, "Actor", Actor())
    monkeypatch.setattr(runtime, "run_actor", run_actor)
    asyncio.run(runtime.main())
    assert messages and "secret" not in repr(messages)


def test_charge_has_bounded_timeout(monkeypatch):
    from shopee_actor import runtime

    calls = []

    class Actor:
        async def charge(self, *args, **kwargs):
            calls.append((args, kwargs))
            await asyncio.sleep(1)

    monkeypatch.setattr(runtime, "CHARGE_TIMEOUT_SECONDS", 0.001)

    async def go():
        with pytest.raises(TimeoutError):
            await runtime.ActorPort(Actor(), charged=True).charge("r:1")

    asyncio.run(go())
    assert len(calls) == 1
