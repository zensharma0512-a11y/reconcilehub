"""Run the server:  python -m reconcilehub --port 8080 --tolerance 1.00"""

from __future__ import annotations

import argparse
import logging
import os

from .http_api import make_server
from .service import ReconciliationService


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reconcilehub", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--tolerance", default="1.00", help="amount tolerance for tier-3 matching")
    parser.add_argument("--base-currency", default="USD", help="currency invoices are issued in")
    parser.add_argument(
        "--audit-log",
        metavar="PATH",
        help="write every applied batch to a tamper-evident chainlog database "
        "(needs: pip install 'reconcilehub[audit]'; HMAC key from $CHAINLOG_KEY)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    audit = None
    if args.audit_log:
        try:
            from chainlog import AuditLog
        except ImportError:
            parser.error("--audit-log needs chainlog: pip install 'reconcilehub[audit]'")
        audit = AuditLog(args.audit_log, key=os.environ.get("CHAINLOG_KEY") or None)
    service = ReconciliationService(tolerance=args.tolerance, base_currency=args.base_currency, audit=audit)
    server = make_server(service, args.host, args.port)
    host, port = server.server_address[:2]
    logging.getLogger("reconcilehub").info("listening on http://%s:%s (tolerance=%s)", host, port, args.tolerance)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if audit is not None:
            audit.close()


if __name__ == "__main__":
    main()
