"""Concurrency stress tests.

Each test releases many threads at once through a barrier so their requests
genuinely overlap, then checks invariants that would break under a race.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from conftest import inv, pay, register
from reconcilehub import ReconciliationService
from reconcilehub.errors import IdempotencyConflict

THREADS = 64


def run_concurrently(fn, n: int = THREADS):
    barrier = threading.Barrier(n)

    def wrapped(i: int):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(wrapped, range(n)))


def test_many_payments_race_for_one_invoice_only_one_wins():
    svc = ReconciliationService()
    register(svc, inv("ONLY", "100"))
    outs = run_concurrently(lambda i: svc.process_payments(f"k{i}", {"payments": [pay(f"P{i}", "100")]}))
    winners = [o for o in outs if o.body["results"][0]["status"] == "matched"]
    assert len(winners) == 1
    assert svc.get_invoice("ONLY")["settled_by"] == winners[0].body["results"][0]["payment_id"]


def test_no_invoice_is_ever_settled_twice_under_load():
    svc = ReconciliationService()
    register(svc, *[inv(f"I{i}", str(100 + i % 10)) for i in range(200)])

    def worker(t: int):
        payments = [pay(f"T{t}-P{j}", str(100 + (t + j) % 10)) for j in range(10)]
        return svc.process_payments(f"k{t}", {"payments": payments})

    outs = run_concurrently(worker)
    matched = [r["invoice_id"] for o in outs for r in o.body["results"] if r["invoice_id"]]
    assert len(matched) == len(set(matched))  # no double settlement
    paid = svc.list_invoices("paid")
    assert len(paid) == len(matched)
    # Every paid invoice points at a payment that points back at it.
    for invoice in paid:
        assert svc.get_payment(invoice["settled_by"])["invoice_id"] == invoice["id"]


def test_same_idempotency_key_concurrently_executes_once():
    svc = ReconciliationService()
    register(svc, inv("A", "100"))
    body = {"payments": [pay("P1", "100")]}
    outs = run_concurrently(lambda i: svc.process_payments("shared", body))
    assert Counter(o.replayed for o in outs) == {True: THREADS - 1, False: 1}
    assert all(o.body == outs[0].body for o in outs)
    assert svc.stats()["payments"] == 1


def test_same_key_conflicting_bodies_concurrently():
    svc = ReconciliationService()

    def worker(i: int):
        try:
            return svc.register_invoices("shared", {"invoices": [inv(f"I{i}", "1")]}).status
        except IdempotencyConflict:
            return 409

    statuses = run_concurrently(worker)
    assert Counter(statuses) == {201: 1, 409: THREADS - 1}
    assert len(svc.list_invoices()) == 1


def test_overlapping_invoice_batches_are_all_or_nothing():
    svc = ReconciliationService()
    # Every batch shares invoice "SHARED", so exactly one batch can win.
    outs = run_concurrently(
        lambda i: svc.register_invoices(f"k{i}", {"invoices": [inv("SHARED", "1"), inv(f"OWN-{i}", "2")]})
    )
    assert Counter(o.status for o in outs) == {201: 1, 409: THREADS - 1}
    ids = {i["id"] for i in svc.list_invoices()}
    assert len(ids) == 2 and "SHARED" in ids  # no orphaned OWN-* from losing batches


def test_reads_during_writes_never_see_partial_batches():
    svc = ReconciliationService()
    register(svc, *[inv(f"I{i}", "10") for i in range(500)])
    stop = threading.Event()
    bad: list[int] = []

    def reader():
        while not stop.is_set():
            n = len(svc.list_invoices("paid"))
            if n % 50:  # writers settle in batches of 50
                bad.append(n)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()
    try:
        run_concurrently(
            lambda i: svc.process_payments(f"k{i}", {"payments": [pay(f"B{i}-{j}", "10") for j in range(50)]}),
            n=10,
        )
    finally:
        stop.set()
        for t in readers:
            t.join()
    assert bad == []
    assert len(svc.list_invoices("paid")) == 500
