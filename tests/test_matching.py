from __future__ import annotations

from conftest import inv, pay, register, settle
from reconcilehub import ReconciliationService


def test_memo_beats_exact_amount(svc):
    register(svc, inv("INV-1", "100"), inv("INV-2", "50"))
    [r] = settle(svc, pay("P1", "50", memo="Payment for INV-1"))
    assert (r["invoice_id"], r["match_tier"], r["difference"]) == ("INV-1", "memo", "-50.00")
    assert svc.get_invoice("INV-2")["status"] == "unpaid"


def test_memo_first_unpaid_id_in_memo_order_wins(svc):
    register(svc, inv("INV-1", "10"), inv("INV-2", "20"), inv("INV-3", "30"))
    settle(svc, pay("P0", "999", memo="INV-2"))  # INV-2 now paid
    [r] = settle(svc, pay("P1", "999", memo="INV-2, INV-3 and INV-1"))
    assert r["invoice_id"] == "INV-3"


def test_memo_is_case_sensitive_and_token_based(svc):
    register(svc, inv("INV-1", "10"))
    [r] = settle(svc, pay("P1", "77", memo="inv-1 / XINV-1 / INV-10"))
    assert r["status"] == "unmatched"


def test_memo_to_unknown_or_paid_falls_through_to_amount(svc):
    register(svc, inv("INV-1", "10"), inv("INV-2", "40"))
    settle(svc, pay("P0", "10", memo="INV-1"))
    [r] = settle(svc, pay("P1", "40", memo="INV-1 INV-999"))
    assert (r["invoice_id"], r["match_tier"]) == ("INV-2", "exact_amount")


def test_exact_tie_break_earliest_due_date(svc):
    register(svc, inv("C", "100", "2026-03-01"), inv("B", "100"), inv("A", "100", "2026-01-15"))
    [r] = settle(svc, pay("P1", "100"))
    assert r["invoice_id"] == "A"


def test_exact_tie_break_registration_order_then_id(svc):
    register(svc, inv("Z", "100", "2026-01-01"))
    register(svc, inv("A", "100", "2026-01-01"))
    [r] = settle(svc, pay("P1", "100"))
    assert r["invoice_id"] == "Z"  # registered first beats alphabetical


def test_exact_beats_closer_tolerance_candidates(svc):
    register(svc, inv("NEAR", "99.99"), inv("EXACT", "100.00", "2027-01-01"))
    [r] = settle(svc, pay("P1", "100"))
    assert (r["invoice_id"], r["match_tier"]) == ("EXACT", "exact_amount")


def test_tolerance_picks_smallest_difference(svc):
    register(svc, inv("FAR", "99.10"), inv("CLOSE", "100.30"), inv("OUT", "101.50"))
    [r] = settle(svc, pay("P1", "100.00"))
    assert (r["invoice_id"], r["match_tier"], r["difference"]) == ("CLOSE", "tolerance", "-0.30")


def test_tolerance_equal_difference_uses_due_date_then_order(svc):
    register(svc, inv("OVER", "100.50"), inv("UNDER", "99.50", "2026-05-01"))
    [r] = settle(svc, pay("P1", "100.00"))
    assert r["invoice_id"] == "UNDER"


def test_tolerance_boundary_is_inclusive():
    svc = ReconciliationService(tolerance="0.50")
    register(svc, inv("A", "100.50"), inv("B", "200.51"))
    r1, r2 = settle(svc, pay("P1", "100.00"), pay("P2", "200.00"))
    assert r1["invoice_id"] == "A"
    assert r2["status"] == "unmatched"


def test_zero_tolerance_disables_tier_three():
    svc = ReconciliationService(tolerance="0")
    register(svc, inv("A", "100.01"))
    [r] = settle(svc, pay("P1", "100.00"))
    assert r["status"] == "unmatched"


def test_invoice_settled_at_most_once_within_a_batch(svc):
    register(svc, inv("A", "100"))
    r1, r2, r3 = settle(svc, pay("P1", "100"), pay("P2", "100"), pay("P3", "1", memo="A"))
    assert r1["invoice_id"] == "A"
    assert r2["status"] == "unmatched" and r3["status"] == "unmatched"
    assert svc.get_invoice("A")["settled_by"] == "P1"


def test_invoice_settled_at_most_once_across_batches(svc):
    register(svc, inv("A", "100"), inv("B", "100"))
    [r1] = settle(svc, pay("P1", "100"))
    [r2] = settle(svc, pay("P2", "100"))
    [r3] = settle(svc, pay("P3", "100"))
    assert {r1["invoice_id"], r2["invoice_id"]} == {"A", "B"}
    assert r3["status"] == "unmatched"


def test_matching_is_deterministic_across_fresh_services():
    def run():
        svc = ReconciliationService()
        register(svc, *[inv(f"I{i}", str(100 + (i % 5)), f"2026-0{1 + i % 3}-01") for i in range(30)])
        return settle(svc, *[pay(f"P{i}", str(100 + (i % 7) * 0.5)[:6]) for i in range(30)])

    assert run() == run()


def test_listing_and_lookup(svc):
    register(svc, inv("A", "1"), inv("B", "2"))
    settle(svc, pay("P1", "1"))
    assert [i["id"] for i in svc.list_invoices("paid")] == ["A"]
    assert [i["id"] for i in svc.list_invoices("unpaid")] == ["B"]
    assert svc.get_payment("P1")["invoice_id"] == "A"
