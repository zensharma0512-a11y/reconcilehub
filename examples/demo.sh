#!/usr/bin/env bash
# Walk through every ReconcileHub feature against a running server.
#   Terminal 1:  python -m reconcilehub --port 8080
#   Terminal 2:  ./examples/demo.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
post() { # post <path> <idempotency-key> <json>
  curl -sS -i -X POST "$BASE$1" -H 'Content-Type: application/json' -H "Idempotency-Key: $2" -d "$3" \
    | sed -n '1p;/^Idempotent-Replayed/p;/^\r$/,$p'; echo
}

step "1. Register invoices (201)"
post /invoices/batch inv-batch-1 '{"invoices":[
  {"id":"INV-1001","amount":"1200.00","due_date":"2026-12-01"},
  {"id":"INV-1002","amount":"450.00"},
  {"id":"INV-1003","amount":"450.00","due_date":"2026-11-20"}]}'

step "2. Retry with the same key -> replayed, not re-executed"
post /invoices/batch inv-batch-1 '{"invoices":[
  {"id":"INV-1001","amount":"1200.00","due_date":"2026-12-01"},
  {"id":"INV-1002","amount":"450.00"},
  {"id":"INV-1003","amount":"450.00","due_date":"2026-11-20"}]}'

step "3. Same key, different body -> 409 IDEMPOTENCY_KEY_CONFLICT"
post /invoices/batch inv-batch-1 '{"invoices":[{"id":"INV-9","amount":"1"}]}'

step "4. Payments: memo, exact (tie -> earliest due date), tolerance, unmatched"
post /payments/batch pay-batch-1 '{"payments":[
  {"id":"PAY-1","amount":"1000.00","memo":"Part payment INV-1001"},
  {"id":"PAY-2","amount":"450.00"},
  {"id":"PAY-3","amount":"449.60"},
  {"id":"PAY-4","amount":"5.00"}]}'

step "5. One bad item -> whole batch rejected (422), nothing applied"
post /payments/batch pay-batch-2 '{"payments":[{"id":"PAY-5","amount":"10"},{"id":"PAY-6","amount":"-3"}]}'

step "6. Re-sending an already-processed payment under a new key -> 409"
post /payments/batch pay-batch-3 '{"payments":[{"id":"PAY-1","amount":"1000.00"}]}'

step "7. Read back"
curl -sS "$BASE/invoices?status=paid"; echo
curl -sS "$BASE/health"; echo
