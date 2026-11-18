# ReconcileHub

[![CI](https://github.com/zensharma0512-a11y/reconcilehub/actions/workflows/ci.yml/badge.svg)](https://github.com/zensharma0512-a11y/reconcilehub/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%E2%80%933.13-blue)
![Dependencies](https://img.shields.io/badge/runtime%20dependencies-0-brightgreen)

A thread-safe service that matches incoming payments to open invoices.
Invoices are registered in batches, and each invoice can be settled at most once.
Payments can arrive in any currency: they are converted to the invoice currency
through the best route in an exchange-rate book before matching.
Clients can retry safely with idempotency keys, and every batch is applied
completely or not at all. The REST API is built on the Python standard library
alone, with no frameworks and no runtime dependencies.

```
POST /invoices/batch   ──►  validate all ──► register all (or none)
POST /payments/batch   ──►  validate all ──► for each payment:
                                                memo ID? ─► exact amount? ─► within tolerance? ─► unmatched
                                             settle + remove from index (atomic, under lock)
                                             any failure ─► undo journal ─► state unchanged
```

## Highlights

| Requirement | How it's met | Proven by |
|---|---|---|
| Input parsing & validation | JSON numbers are parsed straight to `Decimal` (no float rounding). Every field error is reported at once, with paths like `payments[3].amount`. Unknown fields are rejected. | `tests/test_validation.py` |
| Tiered matching | Payments are matched by memo ID first, then by exact amount, then by amount within a tolerance. An amount index (hash map plus a sorted list searched with `bisect`) means matching never scans every invoice. | `tests/test_matching.py` |
| Deterministic tie-breaking | Ties go to the smallest difference, then the earliest due date, then the earliest registration, then the invoice ID | `test_matching_is_deterministic_across_fresh_services` |
| Settle at most once | An invoice is marked paid and removed from the unpaid index in the same critical section | `test_many_payments_race_for_one_invoice_only_one_wins` |
| Idempotent retries | A request is fingerprinted with SHA-256 of its canonical JSON. A retry with the same key and body gets the stored response replayed. The same key with a different body gets **409**. | `test_idempotency_and_atomicity.py` |
| All-or-nothing batches | Everything is validated before any change is made. Changes are recorded in an undo journal, which is rolled back if the batch crashes part-way. | Fault-injection tests that crash mid-batch |
| Thread safety | A single lock covers check, execute and store. Stress tests use 64 threads released together through a barrier. | `tests/test_concurrency.py` (fails if the lock is removed) |
| Multi-currency payments | Directed exchange-rate quotes are registered in atomic batches. A foreign payment is converted along the best route (searching simple paths, so arbitrage loops can't break it) and the rate and route are recorded on the result. A payment with no route rejects its whole batch. | `tests/test_fx.py` |
| Deterministic FX routes | Rates within 1e-9 (relative) of the best count as equal; then fewer hops win, then the alphabetically smallest path. Errors in a rate batch are always reported in the same fixed order. | `test_near_ties_prefer_fewer_hops_then_alphabetical_path`, `test_errors_come_in_fixed_order` |
| Tamper-evident audit trail (optional) | Every applied batch is appended to a [chainlog](https://github.com/zensharma0512-a11y/chainlog) hash chain as the last step of the batch, inside the undo journal. If the audit write fails, the batch rolls back, so nothing is settled without a record. Editing a stored entry later breaks the chain. | `tests/test_audit.py` |
| REST and status codes | 200, 201, 400, 404, 405 (with an `Allow` header), 409, 411, 413, 415, 422, 500 | `tests/test_http.py` |

**131 tests, about 7 seconds.**

## Quick start

```bash
git clone https://github.com/zensharma0512-a11y/reconcilehub.git
cd reconcilehub
python -m pip install -e ".[dev]"     # only pytest is installed, for tests
python -m pytest                       # run the tests
python -m reconcilehub --port 8080 --tolerance 1.00 --base-currency USD
```

Then, in another terminal, run `./examples/demo.sh` to try every endpoint.

### With an audit trail

The core has no runtime dependencies. The audit trail is an optional extra
that uses [chainlog](https://github.com/zensharma0512-a11y/chainlog), a
tamper-evident log I wrote as a separate library:

```bash
python -m pip install -e ".[audit]"
export CHAINLOG_KEY=change-me          # HMAC key, so the chain can't be rebuilt without it
python -m reconcilehub --audit-log audit.db

# later, or from a cron job:
CHAINLOG_DB=audit.db chainlog verify   # OK: 12 entries verified
CHAINLOG_DB=audit.db chainlog tail -n 5
```

Each applied batch becomes one entry: `invoices.registered` (IDs and amounts)
or `payments.processed` (every result, including the FX rate and route used).
Replays and rejected batches change nothing, so they are not logged. From
Python, pass any object with an `append(actor, action, payload)` method:

```python
from chainlog import AuditLog
service = ReconciliationService(audit=AuditLog("audit.db", key="change-me"))
```

## API

All write endpoints **require** an `Idempotency-Key` header. Amounts can be
sent as JSON numbers or strings, and are always returned as strings (`"120.50"`).

### `POST /invoices/batch` → `201 Created`

```json
{ "invoices": [
    { "id": "INV-1001", "amount": "1200.00", "due_date": "2026-12-01" },
    { "id": "INV-1002", "amount": "450.00" }
] }
```

If any ID already exists, the whole batch is rejected with **409 `DUPLICATE_INVOICE`** and nothing is registered.

### `POST /payments/batch` → `200 OK`

```json
{ "payments": [
    { "id": "PAY-1", "amount": "1200.00", "memo": "Settles INV-1001" },
    { "id": "PAY-2", "amount": "449.60" }
] }
```

```json
{ "summary": { "payments": 2, "matched": 2, "unmatched": 0, "tolerance": "1.00" },
  "results": [
    { "payment_id": "PAY-1", "status": "matched", "invoice_id": "INV-1001", "match_tier": "memo",      "difference": "0.00", ... },
    { "payment_id": "PAY-2", "status": "matched", "invoice_id": "INV-1002", "match_tier": "tolerance", "difference": "-0.40", ... }
] }
```

A payment can carry a `currency`. If it isn't the base currency, the result shows
what happened:

```json
{ "payment_id": "PAY-7", "amount": "1000.00", "currency": "EUR", "invoice_id": "INV-2001",
  "match_tier": "exact_amount", "difference": "0.00",
  "fx": { "rate": "1.0922", "route": ["EUR", "GBP", "USD"], "converted_amount": "1092.20" } }
```

An unmatched payment is a normal result, not an error. If a payment ID was already
processed by an earlier request, the batch is rejected with **409 `DUPLICATE_PAYMENT`**.

### `POST /fx/rates/batch` → `201 Created`

```json
{ "rates": [
    { "from": "EUR", "to": "USD", "rate": "1.08" },
    { "from": "EUR", "to": "GBP", "rate": "0.86" },
    { "from": "GBP", "to": "USD", "rate": "1.27" }
] }
```

A quote is directed: `EUR → USD` says nothing about `USD → EUR`. A later batch
can replace a quote. One invalid record rejects the whole batch.

| Query | Returns |
|---|---|
| `GET /fx/rates` | Every registered quote |
| `GET /fx/rates/{FROM}/{TO}` | The direct quote |
| `GET /fx/routes/{FROM}/{TO}` | The direct quote plus every route through one intermediate currency, ascending by rate |
| `GET /fx/best/{FROM}/{TO}` | The best rate over all simple paths of up to 4 conversions |

### Reads

`GET /invoices`, `GET /invoices?status=paid|unpaid`, `GET /invoices/{id}`,
`GET /payments/{id}`, `GET /health`

### Errors

Every error uses the same shape:

```json
{ "error": { "code": "VALIDATION_FAILED", "message": "2 validation error(s); nothing was applied",
             "details": [ { "field": "payments[1].amount", "message": "must be greater than 0" } ] } }
```

| Status | Code | When |
|---|---|---|
| 400 | `MALFORMED_REQUEST` | The body isn't JSON, or has the wrong top-level shape |
| 400 | `MISSING_IDEMPOTENCY_KEY` | A write request has no `Idempotency-Key` |
| 404 | `NOT_FOUND` / `ROUTE_NOT_FOUND` | The resource or route doesn't exist |
| 405 | `METHOD_NOT_ALLOWED` | Wrong method for the route (the response includes an `Allow` header) |
| 409 | `IDEMPOTENCY_KEY_CONFLICT` | The same key was sent with a different request |
| 409 | `DUPLICATE_INVOICE` / `DUPLICATE_PAYMENT` | The ID already exists or was already processed |
| 404 | `NOT_FOUND` | No FX quote or route between the two currencies |
| 422 | `FX_RATE_UNAVAILABLE` | A payment's currency can't be converted to the base currency (nothing is applied) |
| 411 / 413 / 415 | — | `Content-Length` is missing, the body is over 1 MiB, or the content type isn't JSON |
| 422 | `VALIDATION_FAILED` | One or more fields are invalid |
| 500 | `INTERNAL_ERROR` | Unexpected error. The outcome is **not** stored, so retrying is safe. |

## Matching rules

1. **Memo.** The memo is split into ID-like tokens, and the first token that
   names an *unpaid* invoice wins. Matching is case-sensitive and exact, so
   `INV-10` doesn't match `INV-1`. A memo that names a paid or unknown invoice
   falls through to the amount tiers.
2. **Exact amount.** If several unpaid invoices have exactly the payment amount,
   the tie goes to the earliest `due_date` (invoices with no due date sort last),
   then the earliest registration, then the invoice ID.
3. **Tolerance.** The invoice with `|payment − invoice| ≤ tolerance` and the
   smallest difference wins. Remaining ties use the same rules as tier 2. The
   boundary is inclusive, and `--tolerance 0` turns this tier off.

The `difference` field (payment minus invoice) shows underpayments and overpayments.

## Design decisions

**Why one coarse lock?** A payment can match *any* unpaid invoice, so matching
needs a consistent view of the whole index. Per-invoice locks would add
lock-ordering complexity and deadlock risk for little gain, because every
operation is in-memory and takes microseconds. In this design the idempotency
check, the execution and storing the result are one critical section, so two
concurrent requests with the same key can never both execute.

**Why validate first *and* keep an undo journal?** Validating first handles every
failure we can predict. The journal covers the ones we can't, such as a bug or
an exception part-way through a batch. Tests inject a crash at item *n* and check
that the state is unchanged, *including the amount index*: the same payments
match the same invoices afterwards.

**Which outcomes are stored for replay?** Successes and deterministic business
errors (4xx) are stored, so a retry always gets the same answer. Unexpected 500s
are *not* stored, so the client can retry once the fault is fixed.

**Why `Decimal` everywhere?** In floating point, `0.1 + 0.2 != 0.3`. Money is
parsed directly from the JSON text into `Decimal`, and is always serialised as
a string.

**Why search simple paths for the best FX rate?** Taking `-log(rate)` turns
"best rate" into "shortest path", but if some loop of conversions multiplies to
more than 1 (an arbitrage cycle), going round it forever keeps improving the
"rate", and Bellman-Ford or Dijkstra stop giving a meaningful answer. Searching
only simple paths (no currency visited twice) keeps the answer well defined.
The search is exponential in the worst case, so it is capped at 4 conversions,
which is plenty for real currency pairs.

**Why compare FX rates with a tolerance?** Two routes can give rates that differ
only in the 12th decimal place. Treating those as equal, and then preferring
fewer conversions, picks the route a treasury team would actually use, and
keeps the choice stable as quotes are re-registered with tiny changes. All the
arithmetic is `Decimal`, and the rounded rate that is shown is the exact rate
used for conversion, so every result can be reproduced by hand.

**Why reject a batch with an unconvertible payment?** Matching it in the wrong
currency would settle the wrong invoice. Skipping it would quietly split the
batch. Rejecting the batch keeps the all-or-nothing rule.

**How do we know the concurrency tests work?** Removing the lock (and widening
the race window with a 1 ms sleep) makes 4 of the 6 concurrency tests fail. A
test that can't fail proves nothing.

**A real bug the tests caught.** The concurrent HTTP test began failing
intermittently with `ConnectionResetError`. The cause: Python's
`ThreadingHTTPServer` has a default listen backlog of **5**, so a burst of 32
simultaneous clients overflowed the kernel queue. The fix was to raise
`request_queue_size` in the server rather than loosen the test.

**A second one, caught only in CI.** On GitHub's runners the "body too large"
test failed with `BrokenPipeError`. The server replied 413 and closed the
socket while the client was still uploading, so the client never read the
response. Now the server reads and discards an oversized body (capped at 8 MiB)
before it replies.

## Limitations and next steps

- State is held in memory. For production, use PostgreSQL with
  `SELECT … FOR UPDATE SKIP LOCKED` or a unique constraint on
  `settled_by`, and a transactional idempotency table.
- Idempotency records never expire. Add a TTL, such as 24 hours like Stripe uses.
- Invoices are in one base currency. Supporting invoices in several currencies
  would mean adding the currency to every match key.
- Rates have no timestamps. A real treasury setup would convert at the rate in
  force on the payment's value date.
- Partial payments, and payments that cover several invoices, are out of scope.

## Project layout

```
src/reconcilehub/
  validation.py   parsing + field-level validation
  matching.py     tiered matching, tie-breaking, amount index
  fx.py           exchange-rate book, route search, rate-batch validation
  service.py      atomic batches, idempotency, locking, undo journal, audit hook
  http_api.py     REST adapter (http.server, ThreadingHTTPServer)
  errors.py       typed errors -> HTTP status + stable codes
tests/            131 tests: validation, matching, FX, idempotency/atomicity, concurrency, audit, HTTP
```

## License

MIT © Aryan Sharma
