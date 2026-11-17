from __future__ import annotations

import itertools
from typing import Any

import pytest

from reconcilehub import ReconciliationService

_keys = itertools.count()


def key() -> str:
    """A fresh idempotency key."""
    return f"test-key-{next(_keys)}"


def inv(id: str, amount: str, due: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": id, "amount": amount}
    if due is not None:
        d["due_date"] = due
    return d


def pay(id: str, amount: str, memo: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"id": id, "amount": amount}
    if memo is not None:
        d["memo"] = memo
    return d


def register(svc: ReconciliationService, *invoices: dict[str, Any]):
    out = svc.register_invoices(key(), {"invoices": list(invoices)})
    assert out.status == 201, out.body
    return out


def settle(svc: ReconciliationService, *payments: dict[str, Any]):
    out = svc.process_payments(key(), {"payments": list(payments)})
    assert out.status == 200, out.body
    return out.body["results"]


@pytest.fixture
def svc() -> ReconciliationService:
    return ReconciliationService(tolerance="1.00")
