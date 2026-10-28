"""Apify SDK 4.1 integration; the engine remains independently testable."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from apify import Actor, Event
from apify_client import ApifyClientAsync

from shopee_contracts import normalize_input

from .backend import BackendClient, Settings
from .engine import EVENT_NAME, MAX_RUN_TIME, Control, execute

CHARGE_TIMEOUT_SECONDS = 30
STORAGE_TIMEOUT_SECONDS = 30


async def _bounded(awaitable):
    async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
        return await awaitable


def billing_enabled(config, env):
    """Refuse a misconfigured public launch; private tests can opt out explicitly."""
    if not config.is_at_home:
        return bool(config.test_pay_per_event)
    if env.get("SH_ALLOW_UNBILLED_TEST") == "1":
        return False
    pricing = config.actor_pricing_info
    if pricing is not None and pricing.pricing_model == "PAY_PER_EVENT":
        events = pricing.pricing_per_event.actor_charge_events or {}
        event = events.get(EVENT_NAME)
        if (
            event is not None
            and event.event_price_usd is not None
            and event.event_price_usd > 0
            and not event.event_tiered_pricing_usd
            and not event.is_one_time_event
        ):
            return True
    raise ValueError(
        "Configure a repeating product-result pay-per-event price before running publicly"
    )


def identity(config, saved):
    if saved:
        run_id = config.actor_run_id or saved["runId"]
        started = datetime.fromisoformat(saved["deadlineAt"]) - MAX_RUN_TIME
    else:
        run_id = config.actor_run_id or "local-" + uuid4().hex
        started = config.started_at or datetime.now(UTC)
    return run_id, started


class ActorPort:
    def __init__(self, actor, *, charged: bool):
        self.actor = actor
        self.charged = charged
        self._append_client = None

    async def load_state(self):
        return await _bounded(self.actor.get_value("STATE"))

    async def save_state(self, value):
        await _bounded(self.actor.set_value("STATE", value))

    async def existing_result_ids(self):
        async with asyncio.timeout(STORAGE_TIMEOUT_SECONDS):
            dataset = await self.actor.open_dataset()
            rows = [
                row
                async for row in dataset.iterate_items(fields=["resultId"], limit=1001)
            ]
        ids = [row.get("resultId") for row in rows]
        if (
            len(ids) > 1000
            or any(not isinstance(key, str) for key in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("Dataset requires operator reconciliation before resuming")
        return set(ids)

    async def push(self, row):
        config = getattr(self.actor, "configuration", None)
        if config is not None and config.is_at_home:
            if not config.default_dataset_id:
                raise ValueError("Cloud dataset is unavailable")
            if self._append_client is None:
                # Dataset appends have no idempotency key. The default SDK client's
                # automatic retries could append twice before our journal sees an
                # error. Disable transport retries for this non-idempotent effect.
                self._append_client = ApifyClientAsync(
                    token=config.token,
                    api_url=config.api_base_url,
                    api_public_url=config.api_public_base_url,
                    max_retries=0,
                    timeout_medium=timedelta(seconds=20),
                    timeout_max=timedelta(seconds=20),
                )
            await self._append_client.dataset(config.default_dataset_id).push_items(row)
        else:
            await _bounded(self.actor.push_data(row))

    async def close(self):
        if self._append_client is not None:
            await _bounded(self._append_client.http_client.aclose())

    def remaining_budget(self):
        if not self.charged:
            return 1000
        manager = self.actor.get_charging_manager()
        amount = manager.calculate_max_event_charge_count_within_limit(EVENT_NAME)
        return 1000 if amount is None else max(0, min(1000, amount))

    async def charge(self, result_id):
        if not self.charged:
            return True  # Explicit unbilled test, documented separately in SUMMARY.
        # Bound SDK-internal retries well inside the platform's three-minute
        # idempotency window; cancellation leaves a durable uncertain marker.
        async with asyncio.timeout(CHARGE_TIMEOUT_SECONDS):
            result = await self.actor.charge(EVENT_NAME, idempotency_key=result_id)
        return result.charged_count == 1

    async def summarize(self, summary):
        await _bounded(
            self.actor.set_value(
                "SUMMARY",
                {
                    **summary,
                    "billingMode": "pay-per-event" if self.charged else "unbilled-test",
                },
            )
        )
        await _bounded(
            self.actor.set_status_message(
                f"{summary['status']}: delivered {summary['deliveredCount']} of {summary['requestedCount']} products",
                is_terminal=True,
            )
        )


async def run_actor(actor=Actor, *, env=None, backend_factory=BackendClient):
    env = os.environ if env is None else env
    product_ids = normalize_input(await _bounded(actor.get_input()) or {})
    settings = Settings.from_env(env)
    port = ActorPort(actor, charged=billing_enabled(actor.configuration, env))
    run_id, started_at = identity(actor.configuration, await port.load_state())
    control = Control()

    async def migrating(_event=None):
        control.stop("migration")

    async def aborting(_event=None):
        control.stop("abort")

    actor.on(Event.MIGRATING, migrating)
    actor.on(Event.ABORTING, aborting)
    try:
        async with backend_factory(settings) as backend:
            backend.set_control(control)
            summary = await execute(
                port,
                backend,
                product_ids=product_ids,
                run_id=run_id,
                started_at=started_at,
                control=control,
            )
    finally:
        actor.off(Event.MIGRATING, migrating)
        actor.off(Event.ABORTING, aborting)
        await port.close()
    if summary["reason"] == "migration":
        if actor.configuration.is_at_home:
            await actor.reboot()
        return summary
    if summary["status"] != "succeeded" and summary["reason"] not in (
        "budget_exhausted",
        "aborted",
    ):
        await actor.fail(
            status_message="Run incomplete; inspect SUMMARY for preserved results and failures"
        )
    return summary


async def main():
    async with Actor:
        try:
            await run_actor()
        except Exception:  # noqa: BLE001 - external side effects must not leak secrets or be blindly replayed
            # Log only a stable error. SDK traceback logging must not receive raw
            # exceptions from HTTP libraries, configuration or encrypted inputs.
            await Actor.fail(
                status_message="Actor failed; check input, service configuration and SUMMARY"
            )


def entrypoint():
    asyncio.run(main())
