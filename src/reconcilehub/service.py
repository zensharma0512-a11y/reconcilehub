"""The reconciliation service.

Guarantees
----------
* **Atomic batches** - an invoice or payment batch is applied entirely or not
  at all. Validation runs before any mutation; mutations are recorded in an undo
  journal and rolled back if anything fails part-way through.
* **Settle at most once** - a paid invoice leaves the unpaid index in the same
  critical section that marks it paid, so no later payment can see it.
* **Idempotent retries** - a request carries an idempotency key. Replaying the
  same key with the same request returns the stored response without
  re-executing; the same key with a different request is rejected (409).
* **Multi-currency payments** - invoices are in one base currency. A payment
  in another currency is converted with the best route in the FX rate book
  (see ``fx.py``) before matching, and the rate and route are recorded on the
  result. Rates are read under the same lock as the batch, so one batch never
  sees two versions of the book.
* **Thread safety** - every public method runs under one lock. Matching needs a
  consistent view of *all* unpaid invoices (a payment may match any of them),
  so a single coarse lock is the simplest design that is obviously correct.
  Operations are in-memory and microsecond-scale, so contention is low.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Callable

from .errors import (
    DuplicateInvoice,
    DuplicatePayment,
    FxRateUnavailable,
    IdempotencyConflict,
    MissingIdempotencyKey,
    NotFound,
    ReconcileError,
    ValidationFailed,
)
from .fx import MAX_HOPS, RateBook, Route, parse_rate_batch, rate_str
from .matching import UnpaidIndex, find_match
from .models import CENT, Invoice, InvoiceStatus, PaymentInput, PaymentResult, money_str
from .validation import parse_currency, parse_invoice_batch, parse_payment_batch, parse_tolerance

FaultHook = Callable[[str, int], None]
MAX_KEY_LEN = 255


@dataclass(frozen=True)
class Outcome:
    status: int
    body: dict[str, Any]
    replayed: bool = False


@dataclass(frozen=True)
class _IdemRecord:
    fingerprint: str
    status: int
    body: dict[str, Any]


def _fingerprint(operation: str, body: Any) -> str:
    canonical = json.dumps(
        {"op": operation, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,  # Decimal -> its exact string form
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_key(key: str | None) -> str:
    if key is None or key.strip() == "":
        raise MissingIdempotencyKey("An 'Idempotency-Key' header is required for this operation")
    if len(key) > MAX_KEY_LEN or not key.isprintable() or not key.isascii():
        raise ValidationFailed(
            "Invalid idempotency key",
            [{"field": "Idempotency-Key", "message": f"must be 1-{MAX_KEY_LEN} printable ASCII characters"}],
        )
    return key


class ReconciliationService:
    def __init__(
        self,
        tolerance: str | Decimal = "1.00",
        fault_hook: FaultHook | None = None,
        base_currency: str = "USD",
        fx_max_hops: int = MAX_HOPS,
    ) -> None:
        self.tolerance = parse_tolerance(tolerance)
        self.base_currency = parse_currency(base_currency)
        self._fx = RateBook(max_hops=fx_max_hops)
        self._lock = threading.Lock()
        self._invoices: dict[str, Invoice] = {}
        self._unpaid = UnpaidIndex()
        self._payments: dict[str, PaymentResult] = {}
        self._idempotency: dict[str, _IdemRecord] = {}
        self._next_seq = 0
        # Test-only hook used to inject failures mid-batch and prove rollback.
        self._fault_hook = fault_hook

    # ------------------------------------------------------------------ writes

    def register_invoices(self, idempotency_key: str | None, body: Any) -> Outcome:
        return self._idempotent("register_invoices", idempotency_key, body, self._register_invoices)

    def process_payments(self, idempotency_key: str | None, body: Any) -> Outcome:
        return self._idempotent("process_payments", idempotency_key, body, self._process_payments)

    def register_fx_rates(self, idempotency_key: str | None, body: Any) -> Outcome:
        return self._idempotent("register_fx_rates", idempotency_key, body, self._register_fx_rates)

    def _idempotent(
        self,
        operation: str,
        key: str | None,
        body: Any,
        handler: Callable[[Any], tuple[int, dict[str, Any]]],
    ) -> Outcome:
        key = _check_key(key)
        fp = _fingerprint(operation, body)
        # The whole check-execute-store sequence is one critical section, so two
        # concurrent requests with the same key can never both execute.
        with self._lock:
            record = self._idempotency.get(key)
            if record is not None:
                if record.fingerprint != fp:
                    raise IdempotencyConflict(
                        "This Idempotency-Key was already used with a different request"
                    )
                return Outcome(record.status, copy.deepcopy(record.body), replayed=True)

            try:
                status, response = handler(body)
            except ReconcileError as exc:
                # Deterministic business failures are stored too, so a retry gets
                # the same answer instead of a different one.
                status, response = exc.status, exc.to_dict()
            # Unexpected exceptions propagate (-> 500) and are NOT stored, so the
            # client can safely retry; the undo journal has already rolled back.
            self._idempotency[key] = _IdemRecord(fp, status, copy.deepcopy(response))
            return Outcome(status, response)

    def _fault(self, stage: str, i: int) -> None:
        if self._fault_hook is not None:
            self._fault_hook(stage, i)

    def _register_invoices(self, body: Any) -> tuple[int, dict[str, Any]]:
        inputs = parse_invoice_batch(body)  # raises before any mutation

        existing = [inp.id for inp in inputs if inp.id in self._invoices]
        if existing:
            raise DuplicateInvoice(
                f"{len(existing)} invoice(s) already exist; nothing was registered",
                [{"field": "invoices", "message": f"invoice '{i}' already exists"} for i in existing],
            )

        start_seq = self._next_seq
        added: list[Invoice] = []
        try:
            for i, inp in enumerate(inputs):
                inv = Invoice(inp.id, inp.amount, inp.due_date, seq=self._next_seq)
                self._next_seq += 1
                self._invoices[inv.id] = inv
                added.append(inv)
                self._unpaid.add(inv)
                self._fault("register_invoices", i)
        except BaseException:
            for inv in reversed(added):
                self._invoices.pop(inv.id, None)
                try:
                    self._unpaid.remove(inv)
                except KeyError:
                    pass  # failed between dict insert and index insert
            self._next_seq = start_seq
            raise

        return 201, {
            "registered": len(added),
            "invoices": [inv.to_dict() for inv in added],
        }

    def _register_fx_rates(self, body: Any) -> tuple[int, dict[str, Any]]:
        quotes = parse_rate_batch(body)  # raises before any mutation
        count = self._fx.apply(quotes, on_step=lambda i: self._fault("register_fx_rates", i))
        return 201, {
            "registered": count,
            "rates": [{"from": q.source, "to": q.target, "rate": rate_str(q.rate)} for q in quotes],
        }

    def _convert_all(self, inputs: list[PaymentInput]) -> dict[str, tuple[Decimal, Route]]:
        """Convert every foreign-currency payment up front, before any mutation.

        If any payment can't be converted, the whole batch is rejected, the same
        way an invalid field rejects it.
        """
        conversions: dict[str, tuple[Decimal, Route]] = {}
        problems: list[dict[str, str]] = []
        for i, pay in enumerate(inputs):
            currency = pay.currency or self.base_currency
            if currency == self.base_currency:
                continue
            try:
                raw, route = self._fx.convert(pay.amount, currency, self.base_currency)
            except NotFound:
                problems.append({
                    "field": f"payments[{i}].currency",
                    "message": f"no {currency}->{self.base_currency} rate or route is registered",
                })
                continue
            converted = raw.quantize(CENT, rounding=ROUND_HALF_EVEN)
            if converted <= 0:
                problems.append({
                    "field": f"payments[{i}].amount",
                    "message": f"converts to less than 0.01 {self.base_currency}",
                })
                continue
            conversions[pay.id] = (converted, route)
        if problems:
            raise FxRateUnavailable(
                f"{len(problems)} payment(s) can't be converted to {self.base_currency}; nothing was applied",
                problems,
            )
        return conversions

    def _process_payments(self, body: Any) -> tuple[int, dict[str, Any]]:
        inputs = parse_payment_batch(body)  # raises before any mutation

        already = [p.id for p in inputs if p.id in self._payments]
        if already:
            raise DuplicatePayment(
                f"{len(already)} payment(s) were already processed; nothing was applied",
                [{"field": "payments", "message": f"payment '{p}' was already processed"} for p in already],
            )

        conversions = self._convert_all(inputs)

        settled: list[Invoice] = []
        recorded: list[str] = []
        results: list[PaymentResult] = []
        try:
            for i, pay in enumerate(inputs):
                currency = pay.currency or self.base_currency
                fx_info = None
                matchable = pay
                if pay.id in conversions:
                    converted, route = conversions[pay.id]
                    # Match on the amount in the base currency.
                    matchable = PaymentInput(pay.id, converted, pay.memo)
                    fx_info = {
                        "rate": rate_str(route.rate),
                        "route": list(route.path),
                        "converted_amount": money_str(converted),
                    }
                match = find_match(matchable, self._invoices, self._unpaid, self.tolerance)
                if match is None:
                    result = PaymentResult(
                        pay.id, pay.amount, pay.memo, None, None, currency=currency, fx=fx_info
                    )
                else:
                    inv = match.invoice
                    # Mark paid and remove from the index together: once this
                    # runs, no other payment (in this batch or any other) can
                    # match the invoice.
                    self._unpaid.remove(inv)
                    inv.status = InvoiceStatus.PAID
                    inv.settled_by = pay.id
                    inv.match_tier = match.tier
                    settled.append(inv)
                    result = PaymentResult(
                        pay.id,
                        pay.amount,
                        pay.memo,
                        inv.id,
                        match.tier,
                        match.difference(matchable.amount),
                        currency=currency,
                        fx=fx_info,
                    )
                self._payments[pay.id] = result
                recorded.append(pay.id)
                results.append(result)
                self._fault("process_payments", i)
        except BaseException:
            for pid in reversed(recorded):
                del self._payments[pid]
            for inv in reversed(settled):
                inv.status = InvoiceStatus.UNPAID
                inv.settled_by = None
                inv.match_tier = None
                self._unpaid.add(inv)
            raise

        matched = sum(1 for r in results if r.matched)
        return 200, {
            "summary": {
                "payments": len(results),
                "matched": matched,
                "unmatched": len(results) - matched,
                "tolerance": money_str(self.tolerance),
                "base_currency": self.base_currency,
            },
            "results": [r.to_dict() for r in results],
        }

    # ------------------------------------------------------------------- reads

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        with self._lock:
            inv = self._invoices.get(invoice_id)
            if inv is None:
                raise NotFound(f"Invoice '{invoice_id}' not found")
            return inv.to_dict()

    def list_invoices(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {s.value for s in InvoiceStatus}:
            raise ValidationFailed(
                "Invalid status filter",
                [{"field": "status", "message": "must be 'paid' or 'unpaid'"}],
            )
        with self._lock:
            invs = sorted(self._invoices.values(), key=lambda i: i.seq)
            return [i.to_dict() for i in invs if status is None or i.status.value == status]

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        with self._lock:
            result = self._payments.get(payment_id)
            if result is None:
                raise NotFound(f"Payment '{payment_id}' not found")
            return result.to_dict()

    # --------------------------------------------------------------- fx reads

    def fx_quotes(self) -> list[dict[str, str]]:
        with self._lock:
            return self._fx.quotes()

    def fx_direct(self, source: str, target: str) -> dict[str, Any]:
        with self._lock:
            return self._fx.direct(source, target).to_dict()

    def fx_routes(self, source: str, target: str) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._fx.routes(source, target)]

    def fx_best(self, source: str, target: str) -> dict[str, Any]:
        with self._lock:
            return self._fx.best(source, target).to_dict()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            paid = sum(1 for i in self._invoices.values() if i.status is InvoiceStatus.PAID)
            return {
                "invoices": len(self._invoices),
                "invoices_paid": paid,
                "invoices_unpaid": len(self._invoices) - paid,
                "payments": len(self._payments),
                "tolerance": money_str(self.tolerance),
                "base_currency": self.base_currency,
                "fx_quotes": len(self._fx.quotes()),
            }
