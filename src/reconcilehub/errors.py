"""Domain errors.

Every error carries a stable machine-readable ``code`` and the HTTP status the
adapter should map it to, so the HTTP layer never has to guess.
"""

from __future__ import annotations

from typing import Any


class ReconcileError(Exception):
    """Base class for all expected (business) errors."""

    status: int = 400
    code: str = "BAD_REQUEST"

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or []

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            body["error"]["details"] = self.details
        return body


class MalformedRequest(ReconcileError):
    """The body is not parseable JSON or has the wrong top-level shape."""

    status = 400
    code = "MALFORMED_REQUEST"


class ValidationFailed(ReconcileError):
    """The body parsed, but one or more fields are invalid."""

    status = 422
    code = "VALIDATION_FAILED"


class MissingIdempotencyKey(ReconcileError):
    status = 400
    code = "MISSING_IDEMPOTENCY_KEY"


class IdempotencyConflict(ReconcileError):
    """Same idempotency key reused with a different request."""

    status = 409
    code = "IDEMPOTENCY_KEY_CONFLICT"


class DuplicateInvoice(ReconcileError):
    status = 409
    code = "DUPLICATE_INVOICE"


class DuplicatePayment(ReconcileError):
    status = 409
    code = "DUPLICATE_PAYMENT"


class NotFound(ReconcileError):
    status = 404
    code = "NOT_FOUND"
