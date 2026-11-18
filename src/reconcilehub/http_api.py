"""HTTP REST adapter (standard library only).

Routes
------
GET  /health                    liveness + counters
POST /invoices/batch            register invoices atomically      (Idempotency-Key required)
GET  /invoices[?status=paid|unpaid]
GET  /invoices/{id}
POST /payments/batch            match + settle payments atomically (Idempotency-Key required)
GET  /payments/{id}
POST /fx/rates/batch            register exchange-rate quotes atomically (Idempotency-Key required)
GET  /fx/rates                  every registered quote
GET  /fx/rates/{FROM}/{TO}      the direct quote
GET  /fx/routes/{FROM}/{TO}     direct + one-intermediate routes, ascending by rate
GET  /fx/best/{FROM}/{TO}       best rate over all simple paths

Status codes
------------
200 OK, 201 Created, 400 malformed JSON / missing key, 404 unknown resource or
route, 405 wrong method (with Allow header), 409 duplicate or idempotency
conflict, 411 missing Content-Length, 413 body too large, 415 not JSON,
422 validation failed, 500 unexpected error (never stored for replay).
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .errors import ReconcileError
from .service import ReconciliationService
from .validation import parse_json

log = logging.getLogger("reconcilehub.http")

MAX_BODY_BYTES = 1_048_576  # 1 MiB
MAX_DRAIN_BYTES = 8 * MAX_BODY_BYTES  # read & discard at most this much of an oversized body

_ROUTES: list[tuple[re.Pattern[str], dict[str, str]]] = [
    (re.compile(r"^/health$"), {"GET": "health"}),
    (re.compile(r"^/invoices/batch$"), {"POST": "post_invoices"}),
    (re.compile(r"^/invoices$"), {"GET": "list_invoices"}),
    (re.compile(r"^/invoices/(?P<id>[^/]+)$"), {"GET": "get_invoice"}),
    (re.compile(r"^/payments/batch$"), {"POST": "post_payments"}),
    (re.compile(r"^/payments/(?P<id>[^/]+)$"), {"GET": "get_payment"}),
    (re.compile(r"^/fx/rates/batch$"), {"POST": "post_fx_rates"}),
    (re.compile(r"^/fx/rates$"), {"GET": "list_fx_rates"}),
    (re.compile(r"^/fx/rates/(?P<src>[^/]+)/(?P<dst>[^/]+)$"), {"GET": "fx_direct"}),
    (re.compile(r"^/fx/routes/(?P<src>[^/]+)/(?P<dst>[^/]+)$"), {"GET": "fx_routes"}),
    (re.compile(r"^/fx/best/(?P<src>[^/]+)/(?P<dst>[^/]+)$"), {"GET": "fx_best"}),
]


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def _error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}}


class ReconcileHandler(BaseHTTPRequestHandler):
    server_version = "ReconcileHub/1.0"
    protocol_version = "HTTP/1.1"
    service: ReconciliationService  # injected by make_server()

    # --------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body, default=_json_default, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _dispatch(self) -> None:
        parts = urlsplit(self.path)
        for pattern, methods in _ROUTES:
            m = pattern.match(parts.path)
            if not m:
                continue
            action = methods.get(self.command)
            if action is None:
                # Drain any body so keep-alive connections stay in sync.
                self._discard_body()
                status, body = _error(405, "METHOD_NOT_ALLOWED", f"{self.command} not allowed on {parts.path}")
                self._send(status, body, {"Allow": ", ".join(sorted(methods))})
                return
            params = {k: unquote(v) for k, v in m.groupdict().items()}
            query = parse_qs(parts.query)
            try:
                result = getattr(self, f"_h_{action}")(params, query)
            except ReconcileError as exc:
                self._send(exc.status, exc.to_dict())
                return
            except Exception:  # pragma: no cover - defensive
                log.exception("Unhandled error on %s %s", self.command, self.path)
                self._send(*_error(500, "INTERNAL_ERROR", "Unexpected server error; safe to retry"))
                return
            status, body, headers = result
            self._send(status, body, headers)
            return
        self._discard_body()
        self._send(*_error(404, "ROUTE_NOT_FOUND", f"No route for {parts.path}"))

    def _drain(self, n: int) -> None:
        while n > 0:
            chunk = self.rfile.read(min(n, 65536))
            if not chunk:
                break
            n -= len(chunk)

    def _discard_body(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 0:
            self._drain(min(length, MAX_DRAIN_BYTES))
        if length > MAX_BODY_BYTES:
            self.close_connection = True

    def _read_json(self) -> tuple[Any, tuple[int, dict[str, Any]] | None]:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.close_connection = True
            return None, _error(411, "LENGTH_REQUIRED", "Content-Length header is required")
        try:
            length = int(raw_len)
            if length < 0:
                raise ValueError
        except ValueError:
            self.close_connection = True
            return None, _error(400, "MALFORMED_REQUEST", "Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            # Drain (up to a cap) before replying: if we answer and close while
            # the client is still sending, it sees "broken pipe" instead of 413.
            self._drain(min(length, MAX_DRAIN_BYTES))
            self.close_connection = True
            return None, _error(413, "PAYLOAD_TOO_LARGE", f"Body must be at most {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length)
        if ctype != "application/json":
            return None, _error(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
        return parse_json(raw), None

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = lambda self: self._dispatch()  # noqa: E731

    # --------------------------------------------------------------- handlers

    def _h_health(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, {"status": "ok", **self.service.stats()}, None

    def _h_list_invoices(self, params: dict[str, str], query: dict[str, list[str]]):
        status = query.get("status", [None])[0]
        return 200, {"invoices": self.service.list_invoices(status)}, None

    def _h_get_invoice(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, self.service.get_invoice(params["id"]), None

    def _h_get_payment(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, self.service.get_payment(params["id"]), None

    def _write(self, op):
        body, err = self._read_json()
        if err is not None:
            return err[0], err[1], None
        outcome = op(self.headers.get("Idempotency-Key"), body)
        headers = {"Idempotent-Replayed": "true"} if outcome.replayed else None
        return outcome.status, outcome.body, headers

    def _h_post_invoices(self, params: dict[str, str], query: dict[str, list[str]]):
        return self._write(self.service.register_invoices)

    def _h_post_payments(self, params: dict[str, str], query: dict[str, list[str]]):
        return self._write(self.service.process_payments)

    def _h_post_fx_rates(self, params: dict[str, str], query: dict[str, list[str]]):
        return self._write(self.service.register_fx_rates)

    def _h_list_fx_rates(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, {"rates": self.service.fx_quotes()}, None

    def _h_fx_direct(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, self.service.fx_direct(params["src"], params["dst"]), None

    def _h_fx_routes(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, {"routes": self.service.fx_routes(params["src"], params["dst"])}, None

    def _h_fx_best(self, params: dict[str, str], query: dict[str, list[str]]):
        return 200, self.service.fx_best(params["src"], params["dst"]), None


class ReconcileServer(ThreadingHTTPServer):
    # The stdlib default listen backlog is 5: a burst of simultaneous clients
    # beyond that gets "connection reset". Found by test_concurrent_http_clients.
    request_queue_size = 128
    daemon_threads = True


def make_server(
    service: ReconciliationService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    handler = type("BoundReconcileHandler", (ReconcileHandler,), {"service": service})
    return ReconcileServer((host, port), handler)
