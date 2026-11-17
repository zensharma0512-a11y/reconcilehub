"""Tiered payment -> invoice matching.

Tiers, in priority order:

1. MEMO       - the memo contains the ID of an *unpaid* invoice. If several
                unpaid IDs appear, the first one in the memo wins.
2. EXACT      - an unpaid invoice has exactly the payment amount.
3. TOLERANCE  - an unpaid invoice is within +/- ``tolerance`` of the payment.

Deterministic tie-breaking (the same input always gives the same match):

* EXACT:     earliest due date (no due date sorts last), then earliest
             registration order, then invoice ID.
* TOLERANCE: smallest absolute difference first, then the EXACT rules.
"""

from __future__ import annotations

import bisect
import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

from .models import Invoice, InvoiceStatus, MatchTier, PaymentInput

MEMO_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_NO_DUE_DATE = dt.date.max


def tie_key(inv: Invoice) -> tuple[dt.date, int, str]:
    return (inv.due_date or _NO_DUE_DATE, inv.seq, inv.id)


class UnpaidIndex:
    """Unpaid invoices indexed by amount.

    Exact lookups are O(1); tolerance lookups use bisect over the sorted list of
    distinct amounts, so they cost O(log n + k) instead of a full scan.
    """

    def __init__(self) -> None:
        self._by_amount: dict[Decimal, dict[str, Invoice]] = {}
        self._amounts: list[Decimal] = []

    def __len__(self) -> int:
        return sum(len(b) for b in self._by_amount.values())

    def add(self, inv: Invoice) -> None:
        bucket = self._by_amount.get(inv.amount)
        if bucket is None:
            bucket = self._by_amount[inv.amount] = {}
            bisect.insort(self._amounts, inv.amount)
        bucket[inv.id] = inv

    def remove(self, inv: Invoice) -> None:
        bucket = self._by_amount[inv.amount]
        del bucket[inv.id]
        if not bucket:
            del self._by_amount[inv.amount]
            i = bisect.bisect_left(self._amounts, inv.amount)
            del self._amounts[i]

    def exact(self, amount: Decimal) -> list[Invoice]:
        return list(self._by_amount.get(amount, {}).values())

    def within(self, low: Decimal, high: Decimal) -> list[Invoice]:
        lo = bisect.bisect_left(self._amounts, low)
        hi = bisect.bisect_right(self._amounts, high)
        out: list[Invoice] = []
        for amount in self._amounts[lo:hi]:
            out.extend(self._by_amount[amount].values())
        return out


@dataclass(frozen=True)
class Match:
    invoice: Invoice
    tier: MatchTier

    def difference(self, payment_amount: Decimal) -> Decimal:
        return payment_amount - self.invoice.amount


def memo_ids(memo: str) -> list[str]:
    """Candidate invoice IDs in a memo, in order of appearance (case-sensitive)."""
    return MEMO_TOKEN_RE.findall(memo)


def find_match(
    payment: PaymentInput,
    invoices: dict[str, Invoice],
    index: UnpaidIndex,
    tolerance: Decimal,
) -> Match | None:
    # Tier 1: memo reference to an unpaid invoice.
    for token in memo_ids(payment.memo):
        inv = invoices.get(token)
        if inv is not None and inv.status is InvoiceStatus.UNPAID:
            return Match(inv, MatchTier.MEMO)

    # Tier 2: exact amount.
    exact = index.exact(payment.amount)
    if exact:
        return Match(min(exact, key=tie_key), MatchTier.EXACT)

    # Tier 3: within tolerance.
    if tolerance > 0:
        near = index.within(payment.amount - tolerance, payment.amount + tolerance)
        if near:
            best = min(near, key=lambda inv: (abs(payment.amount - inv.amount), *tie_key(inv)))
            return Match(best, MatchTier.TOLERANCE)

    return None
