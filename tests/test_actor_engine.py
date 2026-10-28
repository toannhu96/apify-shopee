"""Offline Actor delivery and billing tests: no SDK or service calls."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from shopee_actor.engine import Control, execute

NOW = datetime(2026, 10, 28, tzinfo=UTC)


def result(item="1", sequence=1):
    return {
        "sequence": sequence,
        "itemId": item,
        "status": "succeeded",
        "errorCode": None,
        "result": {
            "item_id": item,
            "shop_id": "2",
            "name": "Fixture",
            "fetchedAt": NOW.isoformat(),
            "cacheHit": False,
        },
    }


class Port:
    def __init__(self, budget=1000):
        self.state = None
        self.rows = []
        self.charges = []
        self.budget = budget
        self.summaries = []
        self.push_error = None
        self.charge_error = None
        self.saved = []

    async def load_state(self):
        return self.state

    async def save_state(self, value):
        self.state = value
        self.saved.append(value)

    async def existing_result_ids(self):
        return {row["resultId"] for row in self.rows}

    async def push(self, row):
        if self.push_error:
            raise self.push_error
        self.rows.append(row)

    def remaining_budget(self):
        return self.budget

    async def charge(self, key):
        self.charges.append(key)
        if self.charge_error:
            raise self.charge_error
        self.budget -= 1
        return True

    async def summarize(self, summary):
        self.summaries.append(summary)


class Backend:
    def __init__(self, results=None):
        self.results = [result()] if results is None else results
        self.ledger = {}
        self.submissions = []
        self.cancelled = []
        self.records = []
        self.done = True
        self.poll_hook = None

    async def submit(self, run_id, ids, deadline):
        self.submissions.append((run_id, ids, deadline))

    async def poll(self, run_id, cursor):
        if self.poll_hook:
            self.poll_hook()
        page = [r for r in self.results if r["sequence"] > cursor]
        return {
            "results": page,
            "nextCursor": max([cursor] + [r["sequence"] for r in page]),
            "done": self.done,
            "status": "completed" if self.done else "running",
        }

    async def deliveries(self, run_id):
        return self.ledger.copy()

    async def record(self, run_id, item_id, result_id, phase):
        self.records.append((item_id, phase))
        self.ledger[item_id] = {
            "itemId": item_id,
            "phase": phase,
            "resultId": result_id,
        }
        return self.ledger[item_id]

    async def cancel(self, run_id, reason):
        self.cancelled.append(reason)


def run(port=None, backend=None, ids=None, control=None, **kwargs):
    p, b = port or Port(), backend or Backend()
    summary = asyncio.run(
        execute(
            p,
            b,
            product_ids=ids or ["1"],
            run_id="run1",
            started_at=NOW,
            control=control or Control(),
            now=lambda: NOW,
            sleep=lambda _: asyncio.sleep(0),
            **kwargs,
        )
    )
    return p, b, summary


def test_one_valid_result_delivered_then_charged_and_allowlisted():
    b = Backend()
    b.results[0]["result"]["affiliate_link"] = "https://secret.invalid"
    p, b, summary = run(backend=b)
    assert p.charges == ["run1:1"]
    assert b.records == [
        ("1", "delivering"),
        ("1", "delivered"),
        ("1", "charging"),
        ("1", "charged"),
    ]
    assert "affiliate_link" not in p.rows[0]
    assert summary["status"] == "succeeded"
    assert summary["deliveredCount"] == 1
    assert p.saved


def test_zero_budget_does_not_dispatch():
    p, b, s = run(port=Port(0))
    assert not b.submissions and not p.charges and not p.rows
    assert s["reason"] == "budget_exhausted"
    assert s["unfinishedIds"] == ["1"]


def test_dispatch_capped_to_budget_and_excluded_ids_reported():
    _p, b, s = run(port=Port(1), ids=["1", "2"])
    assert b.submissions[0][1] == ["1"]
    assert s["unfinishedIds"] == ["2"]
    assert s["status"] == "partial"


def test_failed_product_never_charged():
    b = Backend(
        [
            {
                "sequence": 1,
                "itemId": "1",
                "status": "failed",
                "errorCode": "unavailable",
                "result": None,
            }
        ]
    )
    p, b, s = run(backend=b)
    assert p.charges == p.rows == []
    assert s["failures"] == [{"itemId": "1", "errorCode": "unavailable"}]
    assert s["status"] == "failed"


def test_restart_reconciles_dataset_and_does_not_charge_twice():
    p, b, _ = run()
    run(p, b)
    assert len(p.rows) == len(p.charges) == 1


def test_charge_in_flight_is_not_replayed_even_after_three_minutes():
    p, b = Port(), Backend()
    p.rows = [{"resultId": "run1:1"}]
    b.ledger = {
        "1": {
            "phase": "charging",
            "resultId": "run1:1",
            "updatedAt": (NOW - timedelta(hours=1)).isoformat(),
        }
    }
    p, b, s = run(p, b)
    assert p.charges == [] and len(p.rows) == 1
    assert s["billingUncertainIds"] == ["1"]
    assert b.ledger["1"]["phase"] == "uncertain"


def test_known_delivery_recovered_before_charge():
    p, b = Port(), Backend()
    p.rows = [{"resultId": "run1:1"}]
    b.ledger = {"1": {"phase": "delivering", "resultId": "run1:1"}}
    p, b, s = run(p, b)
    assert len(p.rows) == len(p.charges) == 1
    assert s["deliveredCount"] == 1


def test_ambiguous_delivery_does_not_push_again():
    p, b = Port(), Backend()
    b.ledger = {"1": {"phase": "delivering", "resultId": "run1:1"}}
    p, b, s = run(p, b)
    assert p.rows == p.charges == []
    assert s["deliveryUncertainIds"] == ["1"]
    assert s["status"] == "failed"


def test_push_failure_is_sanitized_and_never_charged():
    p = Port()
    p.push_error = RuntimeError("cookie=secret")
    p, _b, s = run(p)
    assert not p.charges
    assert "secret" not in repr(s)
    assert s["deliveryUncertainIds"] == ["1"]


def test_charge_failure_is_sanitized_and_does_not_replay():
    p, b = Port(), Backend()
    p.charge_error = RuntimeError("token=secret")
    p, b, s = run(p, b)
    run(p, b)
    assert len(p.charges) == 1
    assert s["billingUncertainIds"] == ["1"]
    assert "secret" not in repr(s)


def test_deadline_preserved_and_expired_work_cancelled():
    p = Port()
    p.state = {
        "runId": "run1",
        "productIds": ["1"],
        "selectedIds": ["1"],
        "deadlineAt": (NOW - timedelta(seconds=1)).isoformat(),
        "cursor": 0,
    }
    p, b, s = run(p)
    assert not p.charges
    assert b.cancelled == ["deadline_exceeded"]
    assert s["reason"] == "deadline_exceeded"


def test_migration_preserves_backend_queue():
    c = Control()
    b = Backend([])
    b.done = False
    b.poll_hook = lambda: c.stop("migration")
    _p, b, s = run(backend=b, control=c)
    assert b.cancelled == [] and s["reason"] == "migration"


def test_abort_cancels_backend():
    c = Control()
    c.stop("abort")
    _p, b, s = run(control=c)
    assert b.cancelled == ["aborted"]
    assert s["reason"] == "aborted"


def test_foreign_item_rejected_before_writing():
    p, _b, s = run(backend=Backend([result("999")]))
    assert not p.rows and not p.charges
    assert s["reason"] == "invalid_backend_response"


def test_resume_with_changed_input_fails_before_backend():
    p = Port()
    p.state = {
        "runId": "run1",
        "productIds": ["2"],
        "selectedIds": ["2"],
        "deadlineAt": (NOW + timedelta(hours=2)).isoformat(),
    }
    with pytest.raises(ValueError, match="does not match"):
        run(p)


def test_backend_failure_after_result_keeps_partial_delivery():
    p, b = Port(), Backend([result("1"), result("2", 2)])
    original = b.record

    async def record(run_id, item_id, result_id, phase):
        if item_id == "2":
            raise RuntimeError("secret")
        return await original(run_id, item_id, result_id, phase)

    b.record = record
    p, b, s = run(p, b, ids=["1", "2"])
    assert s["deliveredCount"] == 1
    assert s["status"] == "partial"
    assert s["unfinishedIds"] == ["2"]


def test_restart_after_budget_exhaustion_preserves_delivery_count():
    p, b, _ = run(port=Port(1))
    p, b, s = run(p, b)
    assert s["deliveredCount"] == 1
    assert s["unfinishedIds"] == []


def test_completed_backend_still_paginates_all_results():
    b = Backend([result("1", 1), result("2", 2)])

    async def poll(run_id, cursor):
        page = [x for x in b.results if x["sequence"] > cursor][:1]
        return {
            "results": page,
            "nextCursor": page[-1]["sequence"] if page else cursor,
            "done": True,
        }

    b.poll = poll
    p, b, s = run(backend=b, ids=["1", "2"])
    assert len(p.rows) == len(p.charges) == 2
    assert s["status"] == "succeeded"


def test_migration_already_requested_never_cancels():
    p = Port()
    p.state = {
        "runId": "run1",
        "productIds": ["1"],
        "selectedIds": ["1"],
        "deadlineAt": (NOW + timedelta(hours=2)).isoformat(),
    }
    c = Control()
    c.stop("migration")
    _, b, s = run(p, control=c)
    assert not b.cancelled
    assert s["reason"] == "migration"


@pytest.mark.parametrize("phase", ["charging", "charged"])
def test_lost_ledger_ack_reports_billing_uncertainty(phase):
    p, b = Port(), Backend()
    original = b.record

    async def record(run_id, item_id, result_id, current):
        value = await original(run_id, item_id, result_id, current)
        if current == phase:
            raise RuntimeError("Lost ledger acknowledgement")
        return value

    b.record = record
    p, b, s = run(p, b)
    assert len(p.rows) == 1
    assert s["billingUncertainIds"] == ["1"]
    assert s["deliveredCount"] == 1


def test_uncertain_charge_stops_other_products_even_with_stale_budget():
    p, b = Port(2), Backend([result("1", 1), result("2", 2)])
    p.charge_error = TimeoutError("Server charged but acknowledgement lost")
    p, b, s = run(p, b, ids=["1", "2"])
    assert p.charges == ["run1:1"]
    assert len(p.rows) == 1
    assert b.cancelled == ["billing_uncertain"]
    assert s["reason"] == "billing_uncertain"
    assert s["unfinishedIds"] == ["2"]


def test_restart_unknown_charge_and_ledger_read_failure_is_reported():
    p, b = Port(), Backend()
    p.rows = [{"resultId": "run1:1"}]

    async def unavailable(_):
        raise RuntimeError("Ledger unavailable")

    b.deliveries = unavailable
    p, b, s = run(p, b)
    assert s["billingUncertainIds"] == ["1"]
    assert s["deliveredCount"] == 1
    assert not p.charges
