"""Recoverable result delivery; external effects are injected for offline tests.

Apify dataset writes and event charges are separate operations. Unknown outcomes
are intentionally not replayed: a missed charge is safer than charging twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from shopee_contracts import normalize_output

EVENT_NAME = "product-result"
MAX_RUN_TIME = timedelta(hours=2)
SAFE_CODES = frozenset(
    {
        "unavailable",
        "authentication",
        "verification",
        "rate_limit",
        "transient",
        "invalid_response",
        "signing",
        "input",
        "deadline_exceeded",
        "aborted",
        "no_healthy_accounts",
        "backend_unavailable",
        "cancelled",
        "budget_exhausted",
        "session_unavailable",
        "account_unavailable",
        "internal_error",
    }
)


class InvalidBackendResponse(RuntimeError):
    """The service returned inconsistent or unsafe data."""


@dataclass
class Control:
    """Shutdown flag set by the SDK event listener, without interrupting a write."""

    reason: str | None = None

    def stop(self, reason: str) -> None:
        if self.reason != "abort":
            self.reason = reason


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("A UTC timestamp is required")
    return result.astimezone(UTC)


def _initial_state(saved, run_id, ids, started_at):
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or saved.get("runId") != run_id
            or saved.get("productIds") != ids
        ):
            raise ValueError("Saved run state does not match current input")
        deadline = _utc(saved["deadlineAt"])
        if deadline > started_at + MAX_RUN_TIME:
            raise ValueError("Saved deadline exceeds the run limit")
        return {**saved, "cursor": 0}
    return {
        "runId": run_id,
        "productIds": ids,
        "deadlineAt": (started_at + MAX_RUN_TIME).isoformat(),
        "cursor": 0,
    }


def _row(entry, expected, run_id):
    if not isinstance(entry, dict) or entry.get("itemId") not in expected:
        raise InvalidBackendResponse("Unexpected product")
    item_id = entry["itemId"]
    if entry.get("status") != "succeeded":
        return item_id, None
    value = entry.get("result")
    if (
        not isinstance(value, dict)
        or str(value.get("item_id")) != item_id
        or type(value.get("cacheHit")) is not bool
    ):
        raise InvalidBackendResponse("Invalid product")
    try:
        public = normalize_output(
            value, fetched_at=_utc(value["fetchedAt"]), cache_hit=value["cacheHit"]
        )
    except (KeyError, TypeError, ValueError):
        raise InvalidBackendResponse("Invalid public result") from None
    return item_id, {**public, "resultId": f"{run_id}:{item_id}"}


async def _deliver(port, backend, run_id, item_id, row, existing, ledger):
    """Return (delivered, uncertain kind); never replay an ambiguous side effect."""
    key = row["resultId"]
    record = ledger.get(item_id, {})
    if record and record.get("resultId") != key:
        raise InvalidBackendResponse("Delivery identity mismatch")
    phase = record.get("phase")
    found = key in existing
    if phase in ("charging", "uncertain"):
        if phase == "charging":
            await backend.record(run_id, item_id, key, "uncertain")
        return found, "billing" if found else "delivery"
    if phase == "charged":
        return found, None if found else "delivery"
    if phase not in (None, "delivering", "delivered"):
        raise InvalidBackendResponse("Unknown delivery phase")
    if phase in ("delivering", "delivered") and not found:
        return False, "delivery"
    if not found:
        await backend.record(run_id, item_id, key, "delivering")
        try:
            await port.push(row)
            found = True
        except Exception:  # noqa: BLE001 - external side effects must not leak secrets or be blindly replayed
            # A timed-out write may already be durable. Never replay it here.
            found = key in await port.existing_result_ids()
            if not found:
                return False, "delivery"
    await backend.record(run_id, item_id, key, "delivered")
    if port.remaining_budget() <= 0:
        # A concurrent budget change can leave an unbilled row; never exceed it.
        await backend.record(run_id, item_id, key, "uncertain")
        return True, "billing"
    await backend.record(run_id, item_id, key, "charging")
    try:
        charged = await port.charge(key)
    except Exception:  # noqa: BLE001 - external side effects must not leak secrets or be blindly replayed
        charged = False
    await backend.record(run_id, item_id, key, "charged" if charged else "uncertain")
    return True, None if charged else "billing"


def _summary(ids, outcomes, reason):
    delivered = [key for key, value in outcomes.items() if value.get("delivered")]
    failures = [
        {"itemId": key, "errorCode": value["errorCode"]}
        for key, value in outcomes.items()
        if value.get("errorCode")
    ]
    unfinished = [
        key for key in ids if key not in outcomes or outcomes[key].get("unfinished")
    ]
    billing = [
        key for key, value in outcomes.items() if value.get("uncertain") == "billing"
    ]
    delivery = [
        key for key, value in outcomes.items() if value.get("uncertain") == "delivery"
    ]
    status = "succeeded"
    if failures or unfinished or billing or delivery or reason not in ("completed",):
        status = "partial" if delivered else "failed"
    return {
        "schemaVersion": "1.0",
        "status": status,
        "reason": reason,
        "requestedCount": len(ids),
        "deliveredCount": len(delivered),
        "failedCount": len(failures),
        "unfinishedIds": unfinished,
        "failures": failures,
        "billingUncertainIds": billing,
        "deliveryUncertainIds": delivery,
    }


def _stop_reason(control, deadline, now, budget):
    if control.reason:
        return "aborted" if control.reason == "abort" else "migration"
    if now() >= deadline:
        return "deadline_exceeded"
    if budget <= 0:
        return "budget_exhausted"
    return None


async def execute(
    port,
    backend,
    *,
    product_ids: list[str],
    run_id: str,
    started_at: datetime,
    control: Control,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep=asyncio.sleep,
) -> dict[str, Any]:
    """Execute one Actor run using its original deadline and durable service ledger."""
    state = _initial_state(await port.load_state(), run_id, product_ids, started_at)
    deadline = _utc(state["deadlineAt"])
    existing = await port.existing_result_ids()
    outcomes: dict[str, dict] = {
        key: {
            **state.get("outcomes", {}).get(key, {"uncertain": "billing"}),
            "delivered": True,
        }
        for key in product_ids
        if f"{run_id}:{key}" in existing
    }
    reason = _stop_reason(control, deadline, now, port.remaining_budget())
    if "selectedIds" not in state:
        count = min(len(product_ids), max(0, port.remaining_budget()))
        state = {**state, "selectedIds": product_ids[: int(count)]}
    await port.save_state(state)
    selected = state["selectedIds"]
    if not isinstance(selected, list) or any(i not in product_ids for i in selected):
        raise ValueError("Saved selected products are invalid")
    try:
        if reason:
            if reason != "migration" and (
                reason in ("aborted", "deadline_exceeded") or selected
            ):
                await backend.cancel(run_id, reason)
        elif selected:
            await backend.submit(run_id, selected, state["deadlineAt"])
            ledger = await backend.deliveries(run_id)
            outcomes, reason, state = await _process(
                port,
                backend,
                state,
                run_id,
                selected,
                existing,
                ledger,
                control,
                deadline,
                now,
                sleep,
                outcomes,
            )
    except InvalidBackendResponse:
        reason = "invalid_backend_response"
        await _cancel_quietly(backend, run_id, reason)
    except Exception:  # noqa: BLE001 - external side effects must not leak secrets or be blindly replayed
        # Never expose HTTP response bodies, URLs, secrets, or exception reprs.
        reason = (
            "migration"
            if control.reason == "migration"
            else "aborted"
            if control.reason == "abort"
            else "backend_unavailable"
        )
        if reason != "migration":
            await _cancel_quietly(backend, run_id, reason)
    if reason in (
        "backend_unavailable",
        "invalid_backend_response",
        "migration",
        "aborted",
    ):
        latest = await port.load_state()
        state = latest
        outcomes = {**outcomes, **latest.get("outcomes", {})}
        existing = await port.existing_result_ids()
        outcomes = {
            **outcomes,
            **{
                key: {**outcomes.get(key, {"uncertain": "billing"}), "delivered": True}
                for key in product_ids
                if f"{run_id}:{key}" in existing
            },
        }
    summary = _summary(product_ids, outcomes, reason or "completed")
    await port.save_state({**state, "summary": summary})
    await port.summarize(summary)
    return summary


async def _cancel_quietly(backend, run_id, reason):
    try:
        await backend.cancel(run_id, reason)
    except Exception:  # noqa: BLE001 - external side effects must not leak secrets or be blindly replayed
        # The durable backend deadline still prevents indefinitely running jobs.
        return


async def _process(
    port,
    backend,
    state,
    run_id,
    selected,
    existing,
    ledger,
    control,
    deadline,
    now,
    sleep,
    outcomes,
):
    seen = set()
    while True:
        reason = _stop_reason(control, deadline, now, port.remaining_budget())
        if reason:
            if reason != "migration":
                await backend.cancel(run_id, reason)
            return outcomes, reason, state
        page = await backend.poll(run_id, state["cursor"])
        if not isinstance(page, dict) or not isinstance(page.get("results"), list):
            raise InvalidBackendResponse("Invalid results page")
        if (
            type(page.get("done")) is not bool
            or type(page.get("nextCursor")) is not int
        ):
            raise InvalidBackendResponse("Invalid pagination")
        for entry in page["results"]:
            reason = _stop_reason(control, deadline, now, port.remaining_budget())
            if reason:
                if reason != "migration":
                    await backend.cancel(run_id, reason)
                return outcomes, reason, state
            item_id, row = _row(entry, selected, run_id)
            if item_id in seen:
                continue
            sequence = entry.get("sequence")
            if type(sequence) is not int or sequence <= state["cursor"]:
                raise InvalidBackendResponse("Invalid result sequence")
            if row is not None:
                delivered, uncertain = await _deliver(
                    port, backend, run_id, item_id, row, existing, ledger
                )
                outcomes = {
                    **outcomes,
                    item_id: {"delivered": delivered, "uncertain": uncertain},
                }
                if delivered:
                    existing = existing | {row["resultId"]}
            elif entry.get("status") in ("failed", "unfinished"):
                code = entry.get("errorCode")
                code = code if code in SAFE_CODES else "internal_error"
                outcomes = {
                    **outcomes,
                    item_id: {
                        "errorCode": code,
                        "unfinished": entry["status"] == "unfinished",
                    },
                }
            else:
                raise InvalidBackendResponse("Invalid result status")
            seen = seen | {item_id}
            state = {**state, "cursor": sequence, "outcomes": outcomes}
            await port.save_state(state)
            if row is not None and uncertain == "billing":
                await backend.cancel(run_id, "billing_uncertain")
                return outcomes, "billing_uncertain", state
        if page["nextCursor"] < state["cursor"]:
            raise InvalidBackendResponse("Pagination went backwards")
        state = {**state, "cursor": page["nextCursor"]}
        if page["done"] and (not page["results"] or len(seen) >= len(selected)):
            return outcomes, "completed", state
        await sleep(2)
