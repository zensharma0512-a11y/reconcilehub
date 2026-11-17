from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import inv, key, pay
from reconcilehub.errors import MalformedRequest
from reconcilehub.validation import parse_json, parse_tolerance


def fields(outcome) -> set[str]:
    return {d["field"] for d in outcome.body["error"].get("details", [])}


def test_json_numbers_parse_as_decimal_not_float():
    body = parse_json(b'{"a": 0.1, "b": 10}')
    assert body["a"] == Decimal("0.1") and isinstance(body["a"], Decimal)


@pytest.mark.parametrize("raw", [b"{", b"\xff\xfe", b'{"a": NaN}', b'{"a": Infinity}'])
def test_malformed_json_rejected(raw):
    with pytest.raises(MalformedRequest):
        parse_json(raw)


@pytest.mark.parametrize(
    "amount",
    ["0", "-5", "1.234", "abc", "", "1e3", True, None, [], "1,000.00", "1000000000.00", Decimal("0.001")],
)
def test_bad_amounts_rejected(svc, amount):
    out = svc.register_invoices(key(), {"invoices": [{"id": "INV-1", "amount": amount}]})
    assert out.status == 422
    assert "invoices[0].amount" in fields(out)


@pytest.mark.parametrize("amount,expected", [("12", "12.00"), ("12.5", "12.50"), (Decimal("7.25"), "7.25"), (Decimal("1E+2"), "100.00")])
def test_good_amounts_normalised(svc, amount, expected):
    out = svc.register_invoices(key(), {"invoices": [{"id": "INV-1", "amount": amount}]})
    assert out.status == 201, out.body
    assert out.body["invoices"][0]["amount"] == expected


@pytest.mark.parametrize("bad_id", ["", " INV", "INV 1", "-INV", "x" * 65, 123, None])
def test_bad_ids_rejected(svc, bad_id):
    out = svc.register_invoices(key(), {"invoices": [{"id": bad_id, "amount": "10"}]})
    assert out.status == 422
    assert "invoices[0].id" in fields(out)


def test_all_errors_reported_at_once_with_paths(svc):
    out = svc.register_invoices(
        key(),
        {"invoices": [inv("A", "10"), {"id": "B", "amount": "-1", "ammount": "1"}, inv("A", "5"), {"amount": "3"}]},
    )
    assert out.status == 422
    assert fields(out) == {"invoices[1].amount", "invoices[1].ammount", "invoices[2].id", "invoices[3].id"}
    assert svc.list_invoices() == []  # nothing applied


def test_invalid_due_date(svc):
    out = svc.register_invoices(key(), {"invoices": [inv("A", "10", "2026-02-30")]})
    assert out.status == 422 and "invoices[0].due_date" in fields(out)


def test_empty_and_oversized_batches(svc):
    assert svc.register_invoices(key(), {"invoices": []}).status == 422
    big = [inv(f"I{i}", "1") for i in range(1001)]
    assert svc.register_invoices(key(), {"invoices": big}).status == 422


@pytest.mark.parametrize("body", [[], "x", {"invoices": "nope"}, {"invoices": None}])
def test_wrong_shape_is_400(svc, body):
    assert svc.register_invoices(key(), body).status == 400


def test_unknown_top_level_field(svc):
    out = svc.register_invoices(key(), {"invoices": [inv("A", "1")], "extra": 1})
    assert out.status == 422


def test_payment_validation(svc):
    out = svc.process_payments(
        key(),
        {"payments": [pay("P1", "10", memo=5), pay("P1", "10"), pay("P2", "1", memo="x" * 501), {"id": "P3"}]},
    )
    assert out.status == 422
    assert fields(out) == {"payments[0].memo", "payments[1].id", "payments[2].memo", "payments[3].amount"}


def test_null_memo_is_empty(svc):
    out = svc.process_payments(key(), {"payments": [{"id": "P1", "amount": "1", "memo": None}]})
    assert out.status == 200


@pytest.mark.parametrize("tol", ["-1", "0.001", "NaN", "abc"])
def test_bad_tolerance(tol):
    with pytest.raises(Exception):
        parse_tolerance(tol)
