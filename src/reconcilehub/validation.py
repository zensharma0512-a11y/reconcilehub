"""Input parsing and validation.

Design rules:
* Parse JSON numbers straight into ``Decimal`` (never through float).
* Collect *every* field error in one pass so a client can fix a batch in one
  round-trip, and report each with a JSON path such as ``payments[3].amount``.
* Be strict: unknown fields are errors (catches typos like ``ammount``).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import MalformedRequest, ValidationFailed
from .models import CENT, InvoiceInput, PaymentInput

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
AMOUNT_STR_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
MAX_AMOUNT = Decimal("999999999.99")
MAX_BATCH = 1000
MAX_MEMO = 500

INVOICE_FIELDS = {"id", "amount", "due_date"}
PAYMENT_FIELDS = {"id", "amount", "memo", "currency"}
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def parse_json(raw: bytes) -> Any:
    """Decode a request body. Numbers become Decimal; NaN/Infinity are rejected."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedRequest("Body must be UTF-8 encoded JSON") from exc
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise MalformedRequest(f"Body is not valid JSON: {exc}") from exc


class _Errors:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"field": path, "message": message})

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationFailed(
                f"{len(self.items)} validation error(s); nothing was applied",
                self.items,
            )


def _parse_id(value: Any, path: str, errors: _Errors) -> str | None:
    if not isinstance(value, str) or not ID_RE.match(value):
        errors.add(path, "must be 1-64 chars of A-Z, a-z, 0-9, '_' or '-', starting alphanumeric")
        return None
    return value


def parse_amount(value: Any, path: str, errors: _Errors) -> Decimal | None:
    # bool is a subclass of int in Python; json gives True/False for true/false.
    if isinstance(value, bool):
        errors.add(path, "must be a number or numeric string, not a boolean")
        return None
    if isinstance(value, str):
        if not AMOUNT_STR_RE.match(value):
            errors.add(path, "must look like '123' or '123.45'")
            return None
        amount = Decimal(value)
    elif isinstance(value, (Decimal, int)):
        amount = Decimal(value)
    else:
        errors.add(path, "must be a number or numeric string")
        return None
    if not amount.is_finite():
        errors.add(path, "must be finite")
        return None
    try:
        if amount != amount.quantize(CENT):
            errors.add(path, "must have at most 2 decimal places")
            return None
    except InvalidOperation:
        errors.add(path, "is out of range")
        return None
    if amount <= 0:
        errors.add(path, "must be greater than 0")
        return None
    if amount > MAX_AMOUNT:
        errors.add(path, f"must be at most {MAX_AMOUNT}")
        return None
    return amount.quantize(CENT)


def _parse_date(value: Any, path: str, errors: _Errors) -> dt.date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.add(path, "must be a 'YYYY-MM-DD' string or null")
        return None
    try:
        if len(value) != 10:
            raise ValueError
        return dt.date.fromisoformat(value)
    except ValueError:
        errors.add(path, "must be a valid 'YYYY-MM-DD' date")
        return None


def _batch_items(body: Any, key: str) -> list[Any]:
    if not isinstance(body, dict):
        raise MalformedRequest(f"Body must be a JSON object with a '{key}' array")
    extra = set(body) - {key}
    if extra:
        raise ValidationFailed(
            "Unknown top-level field(s)",
            [{"field": f, "message": "unknown field"} for f in sorted(extra)],
        )
    items = body.get(key)
    if not isinstance(items, list):
        raise MalformedRequest(f"'{key}' must be an array")
    if not 1 <= len(items) <= MAX_BATCH:
        raise ValidationFailed(
            f"'{key}' must contain between 1 and {MAX_BATCH} items",
            [{"field": key, "message": f"has {len(items)} items"}],
        )
    return items


def _check_object(item: Any, path: str, allowed: set[str], required: set[str], errors: _Errors) -> bool:
    if not isinstance(item, dict):
        errors.add(path, "must be an object")
        return False
    for f in sorted(set(item) - allowed):
        errors.add(f"{path}.{f}", "unknown field")
    for f in sorted(required - set(item)):
        errors.add(f"{path}.{f}", "is required")
    return True


def parse_invoice_batch(body: Any) -> list[InvoiceInput]:
    items = _batch_items(body, "invoices")
    errors = _Errors()
    out: list[InvoiceInput] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(items):
        path = f"invoices[{i}]"
        if not _check_object(item, path, INVOICE_FIELDS, {"id", "amount"}, errors):
            continue
        inv_id = _parse_id(item.get("id"), f"{path}.id", errors) if "id" in item else None
        amount = parse_amount(item.get("amount"), f"{path}.amount", errors) if "amount" in item else None
        due = _parse_date(item.get("due_date"), f"{path}.due_date", errors)
        if inv_id is not None:
            if inv_id in seen:
                errors.add(f"{path}.id", f"duplicates invoices[{seen[inv_id]}].id in this batch")
            else:
                seen[inv_id] = i
        if inv_id is not None and amount is not None:
            out.append(InvoiceInput(inv_id, amount, due))
    errors.raise_if_any()
    return out


def parse_payment_batch(body: Any) -> list[PaymentInput]:
    items = _batch_items(body, "payments")
    errors = _Errors()
    out: list[PaymentInput] = []
    seen: dict[str, int] = {}
    for i, item in enumerate(items):
        path = f"payments[{i}]"
        if not _check_object(item, path, PAYMENT_FIELDS, {"id", "amount"}, errors):
            continue
        pay_id = _parse_id(item.get("id"), f"{path}.id", errors) if "id" in item else None
        amount = parse_amount(item.get("amount"), f"{path}.amount", errors) if "amount" in item else None
        memo = item.get("memo", "")
        if memo is None:
            memo = ""
        if not isinstance(memo, str):
            errors.add(f"{path}.memo", "must be a string")
            memo = ""
        elif len(memo) > MAX_MEMO:
            errors.add(f"{path}.memo", f"must be at most {MAX_MEMO} characters")
        currency = item.get("currency")
        if currency is not None and (not isinstance(currency, str) or not CURRENCY_RE.match(currency)):
            errors.add(f"{path}.currency", "must be a 3-letter upper-case ISO 4217 code, e.g. 'EUR'")
            currency = None
        if pay_id is not None:
            if pay_id in seen:
                errors.add(f"{path}.id", f"duplicates payments[{seen[pay_id]}].id in this batch")
            else:
                seen[pay_id] = i
        if pay_id is not None and amount is not None:
            out.append(PaymentInput(pay_id, amount, memo, currency))
    errors.raise_if_any()
    return out


def parse_currency(value: str) -> str:
    if not isinstance(value, str) or not CURRENCY_RE.match(value):
        raise ValueError("currency must be a 3-letter upper-case ISO 4217 code, e.g. 'USD'")
    return value


def parse_tolerance(value: str | Decimal) -> Decimal:
    tol = Decimal(str(value))
    if not tol.is_finite() or tol < 0 or tol != tol.quantize(CENT):
        raise ValueError("tolerance must be a non-negative amount with at most 2 decimals")
    return tol.quantize(CENT)
