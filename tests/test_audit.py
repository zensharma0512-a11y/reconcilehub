"""Audit trail: every applied batch is logged, nothing else is, and a failed
audit write rolls the batch back."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import inv, key, pay, register, settle
from reconcilehub import ReconciliationService


class ListSink:
    def __init__(self, fail_on: str | None = None) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_on = fail_on

    def append(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> None:
        if action == self.fail_on:
            raise OSError("audit storage unavailable")
        self.entries.append((actor, action, payload or {}))


def test_applied_batches_are_logged_in_order():
    sink = ListSink()
    svc = ReconciliationService(audit=sink)
    register(svc, inv("A", "100"), inv("B", "250.50"))
    settle(svc, pay("P1", "100"), pay("P2", "7"))

    assert [a for _, a, _ in sink.entries] == ["invoices.registered", "payments.processed"]
    assert sink.entries[0][2] == {"invoices": [{"id": "A", "amount": "100.00"}, {"id": "B", "amount": "250.50"}]}
    results = sink.entries[1][2]["results"]
    assert [(r["payment_id"], r["invoice_id"]) for r in results] == [("P1", "A"), ("P2", None)]


def test_replays_and_rejected_batches_are_not_logged():
    sink = ListSink()
    svc = ReconciliationService(audit=sink)
    body = {"invoices": [inv("A", "100")]}
    svc.register_invoices("k1", body)
    svc.register_invoices("k1", body)                          # replay
    svc.register_invoices(key(), {"invoices": [inv("A", "5")]})  # duplicate -> 409
    svc.register_invoices(key(), {"invoices": [inv("X", "-1")]})  # invalid -> 422
    assert len(sink.entries) == 1


def test_failed_audit_write_rolls_back_payments():
    sink = ListSink(fail_on="payments.processed")
    svc = ReconciliationService(audit=sink)
    register(svc, inv("A", "100"))

    with pytest.raises(OSError):
        svc.process_payments(key(), {"payments": [pay("P1", "100")]})

    assert svc.get_invoice("A")["status"] == "unpaid"
    assert svc.stats()["payments"] == 0
    sink.fail_on = None
    assert settle(svc, pay("P1", "100"))[0]["invoice_id"] == "A"  # retry works


def test_failed_audit_write_rolls_back_invoices():
    svc = ReconciliationService(audit=ListSink(fail_on="invoices.registered"))
    with pytest.raises(OSError):
        svc.register_invoices(key(), {"invoices": [inv("A", "100")]})
    assert svc.stats()["invoices"] == 0


def test_works_with_chainlog_and_detects_tampering(tmp_path):
    chainlog = pytest.importorskip("chainlog")
    import sqlite3

    db = tmp_path / "audit.db"
    with chainlog.AuditLog(db, key="secret") as log:
        svc = ReconciliationService(audit=log)
        register(svc, inv("A", "100"))
        settle(svc, pay("P1", "100"))
        assert log.verify().ok and len(log) == 2

    # Someone edits the stored settlement directly in the database file.
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER entries_no_update")
    conn.execute("UPDATE entries SET payload = replace(payload, '\"A\"', '\"B\"') WHERE seq = 2")
    conn.commit()
    conn.close()

    with chainlog.AuditLog(db, key="secret") as log:
        result = log.verify()
    assert not result.ok and result.first_bad_seq == 2
