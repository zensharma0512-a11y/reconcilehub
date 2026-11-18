"""Exchange rates: validation, atomic registration, route queries and
multi-currency payment matching."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from reconcilehub import ReconciliationService
from reconcilehub.errors import IdempotencyConflict, NotFound, ValidationFailed
from reconcilehub.fx import RateBook, parse_rate_batch

from conftest import inv, key, pay, register, settle


def q(src, dst, rate):
    return {"from": src, "to": dst, "rate": rate}


def add_rates(svc, *quotes):
    out = svc.register_fx_rates(key(), {"rates": list(quotes)})
    assert out.status == 201, out.body
    return out


@pytest.fixture
def svc():
    return ReconciliationService(tolerance="1.00", base_currency="INR")


def fields(outcome):
    return [d["field"] for d in outcome.body["error"]["details"]]


# ------------------------------------------------------------------ validation


class TestRateValidation:
    def test_valid_batch(self):
        quotes = parse_rate_batch({"rates": [q("USD", "INR", "83.25"), q("EUR", "USD", Decimal("1.08"))]})
        assert [(x.source, x.target, x.rate) for x in quotes] == [
            ("USD", "INR", Decimal("83.25")),
            ("EUR", "USD", Decimal("1.08")),
        ]

    def test_errors_come_in_fixed_order(self, svc):
        out = svc.register_fx_rates(key(), {"rates": [
            q("usd", "INR", "1"),                     # bad from
            q("USD", "USD", "1"),                     # same currency
            q("USD", "EUR", "0"),                     # rate must be > 0
            {"from": "USD", "rate": "1", "x": 1},     # unknown field, then missing field
            q("GBP", "INR", True),                    # bool is not a rate
            q("JPY", "INR", "0.55"),
            q("JPY", "INR", "0.56"),                  # duplicate pair in batch
            "not an object",
        ]})
        assert out.status == 422
        assert fields(out) == [
            "rates[0].from",
            "rates[1].to",
            "rates[2].rate",
            "rates[3].x",
            "rates[3].to",
            "rates[4].rate",
            "rates[6]",
            "rates[7]",
        ]

    def test_same_bad_input_gives_identical_errors(self):
        body = {"rates": [q("USD", "usd", "-1"), q("EUR", "EUR", "abc")]}
        with pytest.raises(ValidationFailed) as a:
            parse_rate_batch(body)
        with pytest.raises(ValidationFailed) as b:
            parse_rate_batch(body)
        assert a.value.details == b.value.details

    @pytest.mark.parametrize("rate", ["1.1234567890123", "1e3", "-2", "", 1.5, None])
    def test_rejected_rates(self, rate):
        with pytest.raises(ValidationFailed):
            parse_rate_batch({"rates": [q("USD", "INR", rate)]})


# ------------------------------------------------------------------ atomicity


class TestAtomicRegistration:
    def test_one_bad_record_rejects_the_whole_batch(self, svc):
        out = svc.register_fx_rates(key(), {"rates": [q("USD", "INR", "83"), q("EUR", "INR", "nope")]})
        assert out.status == 422
        assert svc.fx_quotes() == []

    def test_crash_mid_batch_leaves_book_unchanged(self):
        def crash(stage, i):
            if stage == "register_fx_rates" and i == 1:
                raise RuntimeError("injected")

        svc = ReconciliationService(base_currency="INR", fault_hook=crash)
        svc._fault_hook = None
        add_rates(svc, q("USD", "INR", "83"))
        svc._fault_hook = crash
        with pytest.raises(RuntimeError):
            svc.register_fx_rates(key(), {"rates": [q("USD", "INR", "84"), q("EUR", "INR", "90")]})
        assert svc.fx_quotes() == [{"from": "USD", "to": "INR", "rate": "83"}]

    def test_later_batch_replaces_a_quote(self, svc):
        add_rates(svc, q("USD", "INR", "83"))
        add_rates(svc, q("USD", "INR", "84.10"))
        assert svc.fx_direct("USD", "INR")["rate"] == "84.1"

    def test_idempotent_retry(self, svc):
        body = {"rates": [q("USD", "INR", "83")]}
        k = key()
        first = svc.register_fx_rates(k, body)
        again = svc.register_fx_rates(k, body)
        assert again.replayed and again.body == first.body
        with pytest.raises(IdempotencyConflict):
            svc.register_fx_rates(k, {"rates": [q("USD", "INR", "99")]})


# --------------------------------------------------------------------- queries


@pytest.fixture
def book():
    b = RateBook()
    b.apply(parse_rate_batch({"rates": [
        q("USD", "INR", "83"),
        q("USD", "EUR", "0.9"),
        q("EUR", "INR", "93"),     # USD->EUR->INR = 83.7, better than direct
        q("USD", "GBP", "0.8"),
        q("GBP", "INR", "105"),    # USD->GBP->INR = 84
        q("GBP", "EUR", "1.2"),
        q("INR", "USD", "0.012"),
    ]}))
    return b


class TestQueries:
    def test_direct(self, book):
        assert book.direct("USD", "INR").to_dict() == {"path": ["USD", "INR"], "hops": 1, "rate": "83"}

    def test_quotes_are_directed(self, book):
        with pytest.raises(NotFound):
            book.direct("INR", "EUR")
        assert book.direct("INR", "USD").rate == Decimal("0.012")

    def test_one_hop_routes_sorted_ascending(self, book):
        routes = [r.to_dict() for r in book.routes("USD", "INR")]
        assert routes == [
            {"path": ["USD", "INR"], "hops": 1, "rate": "83"},
            {"path": ["USD", "EUR", "INR"], "hops": 2, "rate": "83.7"},
            {"path": ["USD", "GBP", "INR"], "hops": 2, "rate": "84"},
        ]

    def test_best_route_uses_longer_paths(self, book):
        # USD->GBP->EUR->INR = 0.8 * 1.2 * 93 = 89.28
        best = book.best("USD", "INR")
        assert best.path == ("USD", "GBP", "EUR", "INR") and best.rate == Decimal("89.28")

    def test_near_ties_prefer_fewer_hops_then_alphabetical_path(self):
        b = RateBook()
        b.apply(parse_rate_batch({"rates": [
            q("AAA", "ZZZ", "2"),
            q("AAA", "CCC", "1"), q("CCC", "ZZZ", "2"),
            q("AAA", "BBB", "1"), q("BBB", "ZZZ", "2"),
        ]}))
        assert b.best("AAA", "ZZZ").path == ("AAA", "ZZZ")  # equal rate, fewest hops

        b2 = RateBook()
        b2.apply(parse_rate_batch({"rates": [
            q("AAA", "CCC", "1"), q("CCC", "ZZZ", "2"),
            q("AAA", "BBB", "1"), q("BBB", "ZZZ", "1.9999999999"),  # within 1e-9 of 2
        ]}))
        assert b2.best("AAA", "ZZZ").path == ("AAA", "BBB", "ZZZ")  # same hops, smaller path

    def test_clearly_better_rate_beats_fewer_hops(self):
        b = RateBook()
        b.apply(parse_rate_batch({"rates": [
            q("AAA", "ZZZ", "2"), q("AAA", "BBB", "1"), q("BBB", "ZZZ", "2.001"),
        ]}))
        assert b.best("AAA", "ZZZ").path == ("AAA", "BBB", "ZZZ")

    def test_arbitrage_cycle_does_not_loop(self):
        b = RateBook()
        b.apply(parse_rate_batch({"rates": [
            q("USD", "EUR", "1.1"), q("EUR", "USD", "1.1"),  # a money-making loop
            q("EUR", "INR", "90"),
        ]}))
        best = b.best("USD", "INR")
        assert best.path == ("USD", "EUR", "INR") and best.rate == Decimal("99")

    def test_max_hops_caps_the_search(self):
        chain = {"rates": [q("AAA", "BBB", "1"), q("BBB", "CCC", "1"), q("CCC", "DDD", "1")]}
        short = RateBook(max_hops=2)
        short.apply(parse_rate_batch(chain))
        with pytest.raises(NotFound):
            short.best("AAA", "DDD")
        assert short.best("AAA", "CCC").hops == 2

    @pytest.mark.parametrize("pair", [("usd", "INR"), ("USD", "USD"), ("USD", "INRR")])
    def test_bad_pairs_are_validation_errors(self, book, pair):
        with pytest.raises(ValidationFailed):
            book.best(*pair)

    def test_unknown_currency_is_not_found(self, book):
        with pytest.raises(NotFound):
            book.best("USD", "JPY")

    def test_results_are_detached(self, svc):
        add_rates(svc, q("USD", "INR", "83"), q("USD", "EUR", "0.9"), q("EUR", "INR", "93"))
        routes = svc.fx_routes("USD", "INR")
        routes[0]["path"].append("XXX")
        routes.clear()
        assert len(svc.fx_routes("USD", "INR")) == 2


# ------------------------------------------------------- multi-currency payments


class TestForeignPayments:
    def test_foreign_payment_is_converted_then_matched(self, svc):
        register(svc, inv("INV-1", "8300.00"))
        add_rates(svc, q("USD", "INR", "83"))
        r = settle(svc, {"id": "PAY-1", "amount": "100.00", "currency": "USD"})[0]
        assert r["status"] == "matched" and r["invoice_id"] == "INV-1" and r["match_tier"] == "exact_amount"
        assert r["amount"] == "100.00" and r["currency"] == "USD"
        assert r["fx"] == {"rate": "83", "route": ["USD", "INR"], "converted_amount": "8300.00"}

    def test_conversion_uses_best_route_and_feeds_tolerance_tier(self, svc):
        register(svc, inv("INV-1", "8370.50"))
        add_rates(svc, q("USD", "INR", "83"), q("USD", "EUR", "0.9"), q("EUR", "INR", "93"))
        r = settle(svc, {"id": "PAY-1", "amount": "100.00", "currency": "USD"})[0]
        assert r["fx"]["route"] == ["USD", "EUR", "INR"] and r["fx"]["converted_amount"] == "8370.00"
        assert r["match_tier"] == "tolerance" and r["difference"] == "-0.50"

    def test_base_currency_payments_are_unchanged(self, svc):
        register(svc, inv("INV-1", "500.00"))
        r = settle(svc, pay("PAY-1", "500.00"))[0]
        assert r["currency"] == "INR" and r["fx"] is None and r["invoice_id"] == "INV-1"

    def test_missing_rate_rejects_whole_batch(self, svc):
        register(svc, inv("INV-1", "100.00"))
        out = svc.process_payments(key(), {"payments": [
            pay("PAY-1", "100.00"),
            {"id": "PAY-2", "amount": "5.00", "currency": "JPY"},
        ]})
        assert out.status == 422 and out.body["error"]["code"] == "FX_RATE_UNAVAILABLE"
        assert fields(out) == ["payments[1].currency"]
        assert svc.get_invoice("INV-1")["status"] == "unpaid"  # PAY-1 was not applied either
        with pytest.raises(NotFound):
            svc.get_payment("PAY-1")

    def test_bad_currency_code_is_validation_error(self, svc):
        out = svc.process_payments(key(), {"payments": [{"id": "P", "amount": "1.00", "currency": "usd"}]})
        assert out.status == 422 and fields(out) == ["payments[0].currency"]

    def test_tiny_conversion_is_rejected(self, svc):
        add_rates(svc, q("VND", "INR", "0.000001"))
        out = svc.process_payments(key(), {"payments": [{"id": "P", "amount": "1.00", "currency": "VND"}]})
        assert out.status == 422 and fields(out) == ["payments[0].amount"]

    def test_rate_updates_and_payments_never_interleave(self, svc):
        """Each payment batch sees one consistent version of the rate book."""
        register(svc, *[inv(f"INV-{i}", "8300.00") for i in range(30)])
        add_rates(svc, q("USD", "INR", "83"))
        barrier = threading.Barrier(31)

        def pay_one(i):
            barrier.wait()
            return settle(svc, {"id": f"PAY-{i}", "amount": "100.00", "currency": "USD"})[0]

        def churn():
            barrier.wait()
            for n in range(50):
                add_rates(svc, q("USD", "INR", "83" if n % 2 else "83.5"))

        with ThreadPoolExecutor(31) as pool:
            churner = pool.submit(churn)
            results = list(pool.map(pay_one, range(30)))
            churner.result()
        for r in results:
            assert r["fx"]["converted_amount"] == {"83": "8300.00", "83.5": "8350.00"}[r["fx"]["rate"]]
