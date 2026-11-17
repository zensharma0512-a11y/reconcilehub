"""End-to-end tests against a real server on an ephemeral port."""

from __future__ import annotations

import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from reconcilehub import ReconciliationService
from reconcilehub.http_api import make_server


@pytest.fixture
def server():
    srv = make_server(ReconciliationService(tolerance="1.00"), "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def call(port, method, path, body=None, key=None, ctype="application/json", raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = ctype
    if key:
        headers["Idempotency-Key"] = key
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, (json.loads(payload) if payload else None), dict(resp.getheaders())


INVOICES = {"invoices": [{"id": "INV-1", "amount": "100.00"}, {"id": "INV-2", "amount": "49.50"}]}


def test_happy_path(server):
    s, b, _ = call(server, "POST", "/invoices/batch", INVOICES, key="i1")
    assert s == 201 and b["registered"] == 2
    s, b, _ = call(server, "POST", "/payments/batch", {"payments": [{"id": "P1", "amount": "50.00"}]}, key="p1")
    assert s == 200 and b["results"][0]["match_tier"] == "tolerance"
    s, b, _ = call(server, "GET", "/invoices/INV-2")
    assert s == 200 and b["status"] == "paid" and b["settled_by"] == "P1"
    s, b, _ = call(server, "GET", "/invoices?status=unpaid")
    assert [i["id"] for i in b["invoices"]] == ["INV-1"]
    s, b, _ = call(server, "GET", "/payments/P1")
    assert s == 200 and b["invoice_id"] == "INV-2"
    s, b, _ = call(server, "GET", "/health")
    assert s == 200 and b["invoices_paid"] == 1


def test_replay_header_and_conflict(server):
    s1, b1, h1 = call(server, "POST", "/invoices/batch", INVOICES, key="same")
    s2, b2, h2 = call(server, "POST", "/invoices/batch", INVOICES, key="same")
    assert (s1, s2) == (201, 201) and b1 == b2
    assert "Idempotent-Replayed" not in h1 and h2["Idempotent-Replayed"] == "true"
    s3, b3, _ = call(server, "POST", "/invoices/batch", {"invoices": [{"id": "X", "amount": "1"}]}, key="same")
    assert s3 == 409 and b3["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


@pytest.mark.parametrize(
    "kwargs,status,code",
    [
        ({"body": INVOICES}, 400, "MISSING_IDEMPOTENCY_KEY"),
        ({"raw": b"{not json", "key": "k"}, 400, "MALFORMED_REQUEST"),
        ({"body": INVOICES, "key": "k", "ctype": "text/plain"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
        ({"body": {"invoices": [{"id": "A", "amount": "-1"}]}, "key": "k"}, 422, "VALIDATION_FAILED"),
    ],
)
def test_error_statuses(server, kwargs, status, code):
    s, b, _ = call(server, "POST", "/invoices/batch", **kwargs)
    assert s == status and b["error"]["code"] == code


def test_duplicate_invoice_is_409(server):
    call(server, "POST", "/invoices/batch", INVOICES, key="a")
    s, b, _ = call(server, "POST", "/invoices/batch", INVOICES, key="b")
    assert s == 409 and b["error"]["code"] == "DUPLICATE_INVOICE"


def test_404s_and_405(server):
    assert call(server, "GET", "/invoices/NOPE")[0] == 404
    assert call(server, "GET", "/payments/NOPE")[0] == 404
    assert call(server, "GET", "/nowhere")[1]["error"]["code"] == "ROUTE_NOT_FOUND"
    s, b, h = call(server, "GET", "/invoices/batch")
    assert s == 405 and h["Allow"] == "POST"
    s, _, h = call(server, "DELETE", "/invoices/INV-1")
    assert s == 405 and h["Allow"] == "GET"


def test_bad_status_filter_is_422(server):
    assert call(server, "GET", "/invoices?status=maybe")[0] == 422


def test_body_too_large_is_413(server):
    big = b'{"invoices":[' + b" " * 1_100_000 + b"]}"
    assert call(server, "POST", "/invoices/batch", raw=big, key="k")[0] == 413


def test_concurrent_http_clients_settle_each_invoice_once(server):
    call(server, "POST", "/invoices/batch", {"invoices": [{"id": "ONLY", "amount": "10"}]}, key="setup")

    def hit(i):
        return call(server, "POST", "/payments/batch", {"payments": [{"id": f"P{i}", "amount": "10"}]}, key=f"k{i}")

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(hit, range(32)))
    assert all(s == 200 for s, _, _ in results)
    matched = [b for _, b, _ in results if b["summary"]["matched"] == 1]
    assert len(matched) == 1
