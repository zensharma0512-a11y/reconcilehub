"""Domain models.

Money is always ``decimal.Decimal`` quantized to cents and serialised as a
string ("120.50"), never as a float, so no precision is lost on the wire.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

CENT = Decimal("0.01")


def money_str(value: Decimal) -> str:
    return str(value.quantize(CENT))


class MatchTier(str, Enum):
    """How a payment was matched. Order of the members == order of priority."""

    MEMO = "memo"            # memo references an unpaid invoice ID
    EXACT = "exact_amount"   # amount equals an unpaid invoice amount
    TOLERANCE = "tolerance"  # amount within +/- tolerance of an unpaid invoice


class InvoiceStatus(str, Enum):
    UNPAID = "unpaid"
    PAID = "paid"


@dataclass(frozen=True)
class InvoiceInput:
    id: str
    amount: Decimal
    due_date: dt.date | None = None


@dataclass(frozen=True)
class PaymentInput:
    id: str
    amount: Decimal
    memo: str = ""


@dataclass
class Invoice:
    id: str
    amount: Decimal
    due_date: dt.date | None
    seq: int  # global registration order, used for deterministic tie-breaking
    status: InvoiceStatus = InvoiceStatus.UNPAID
    settled_by: str | None = None
    match_tier: MatchTier | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "amount": money_str(self.amount),
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "status": self.status.value,
            "settled_by": self.settled_by,
            "match_tier": self.match_tier.value if self.match_tier else None,
        }


@dataclass
class PaymentResult:
    payment_id: str
    amount: Decimal
    memo: str
    invoice_id: str | None
    tier: MatchTier | None
    difference: Decimal | None = field(default=None)  # payment - invoice

    @property
    def matched(self) -> bool:
        return self.invoice_id is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "amount": money_str(self.amount),
            "memo": self.memo,
            "status": "matched" if self.matched else "unmatched",
            "invoice_id": self.invoice_id,
            "match_tier": self.tier.value if self.tier else None,
            "difference": money_str(self.difference) if self.difference is not None else None,
        }
