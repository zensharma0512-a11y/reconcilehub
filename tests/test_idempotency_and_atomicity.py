from __future__ import annotations

import pytest

from conftest import inv, pay, register, settle
from reconcilehub import ReconciliationService
from reconcilehub.errors import IdempotencyConflict, MissingIdempotencyKey, ValidationFailed

# ------------------------------------------------------------- idempotency


def test_replay_returns_identical_response_without_reexecuting(svc):
    register(svc, inv("A", "100"))
    body = {"payments": [pay("P1", "100")]}
    first = svc.process_payments("retry-1", body)
    second = svc.process_payments("retry-1", body)
    assert (first.replayed, second.replayed) == (False, True)
    assert first.status == second.status == 200
    assert first.body == second.body
    assert svc.stats()["payments"] == 1


def test_same_key_different_body_is_conflict(svc):
    register(svc, inv("A", "100"))
    svc.process_payments("k", {"payments": [pay("P1", "100")]})
    with pytest.raises(IdempotencyConflict):
        svc.process_payments("k", {"payments": [pay("P1", "100.01")]})


def test_same_key_different_operation_is_conflict(svc):
    body = {"invoices": [inv("A", "1")]}
    svc.register_invoices("k", body)
    with pytest.raises(IdempotencyConflict):
        svc.process_payments("k", body)


def test_failed_business_outcomes_are_replayed_too(svc):
    bad = {"invoices": [inv("A", "-1")]}
    first = svc.register_invoices("k", bad)
    second = svc.register_invoices("k", bad)
    assert first.status == second.status == 422 and second.replayed


def test_new_key_after_success_reports_duplicate(svc):
    register(svc, inv("A", "100"))
    body = {"payments": [pay("P1", "100")]}
    assert svc.process_payments("k1", body).status == 200
    out = svc.process_payments("k2", body)
    assert out.status == 409 and out.body["error"]["code"] == "DUPLICATE_PAYMENT"


def test_replayed_body_cannot_be_mutated_by_caller(svc):
    body = {"invoices": [inv("A", "1")]}
    first = svc.register_invoices("k", body)
    first.body["registered"] = 999
    assert svc.register_invoices("k", body).body["registered"] == 1


@pytest.mark.parametrize("k", [None, "", "   "])
def test_missing_key(svc, k):
    with pytest.raises(MissingIdempotencyKey):
        svc.register_invoices(k, {"invoices": [inv("A", "1")]})


@pytest.mark.parametrize("k", ["x" * 256, "tab\there", "ключ"])
def test_invalid_key(svc, k):
    with pytest.raises(ValidationFailed):
        svc.register_invoices(k, {"invoices": [inv("A", "1")]})


# --------------------------------------------------------------- atomicity


def test_invoice_batch_with_existing_id_registers_nothing(svc):
    register(svc, inv("A", "1"))
    out = svc.register_invoices("k", {"invoices": [inv("B", "2"), inv("A", "3"), inv("C", "4")]})
    assert out.status == 409 and out.body["error"]["code"] == "DUPLICATE_INVOICE"
    assert [i["id"] for i in svc.list_invoices()] == ["A"]


def test_payment_batch_with_one_invalid_item_applies_nothing(svc):
    register(svc, inv("A", "100"), inv("B", "200"))
    out = svc.process_payments("k", {"payments": [pay("P1", "100"), pay("P2", "-200")]})
    assert out.status == 422
    assert svc.stats()["payments"] == 0
    assert svc.get_invoice("A")["status"] == "unpaid"


def test_payment_batch_with_previously_processed_id_applies_nothing(svc):
    register(svc, inv("A", "100"), inv("B", "200"))
    settle(svc, pay("P1", "5"))
    out = svc.process_payments("k", {"payments": [pay("P2", "100"), pay("P1", "200")]})
    assert out.status == 409
    assert svc.list_invoices("paid") == []


class Boom(RuntimeError):
    pass


def failing_at(stage: str, index: int):
    def hook(s: str, i: int) -> None:
        if s == stage and i == index:
            raise Boom(f"injected failure at {s}[{i}]")

    return hook


def test_mid_batch_crash_rolls_back_payments():
    svc = ReconciliationService(fault_hook=failing_at("process_payments", 2))
    register(svc, inv("A", "100"), inv("B", "200"), inv("C", "300"))
    before = svc.list_invoices()
    with pytest.raises(Boom):
        svc.process_payments("k", {"payments": [pay("P1", "100"), pay("P2", "200"), pay("P3", "300")]})
    assert svc.list_invoices() == before
    assert svc.stats()["payments"] == 0
    # The index was restored too: the same invoices still match afterwards.
    svc._fault_hook = None
    out = svc.process_payments("k", {"payments": [pay("P1", "100"), pay("P2", "200"), pay("P3", "300")]})
    assert out.status == 200 and not out.replayed  # crash was not stored for replay
    assert out.body["summary"]["matched"] == 3


def test_mid_batch_crash_rolls_back_invoice_registration():
    svc = ReconciliationService(fault_hook=failing_at("register_invoices", 1))
    with pytest.raises(Boom):
        svc.register_invoices("k", {"invoices": [inv("A", "1"), inv("B", "2"), inv("C", "3")]})
    assert svc.list_invoices() == []
    svc._fault_hook = None
    register(svc, inv("A", "1"))
    [r] = settle(svc, pay("P1", "2"))
    assert r["status"] == "matched" and r["invoice_id"] == "A"  # via tolerance; B never existed
