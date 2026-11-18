"""Exchange-rate book: directed quotes and route search.

A quote ``USD -> INR @ 83.25`` means 1 USD buys 83.25 INR. Quotes are
*directed*: quoting USD -> INR says nothing about INR -> USD, because real
buy and sell rates differ.

Three queries:

* ``direct(a, b)``: the quoted rate, if there is one.
* ``routes(a, b)``: the direct rate plus every route through exactly one
  intermediate currency, sorted by rate (ascending), then by path.
* ``best(a, b)``: the highest rate over all *simple* paths (no currency visited
  twice) of up to ``max_hops`` conversions. Rates within a small relative
  tolerance of the maximum count as equal, and then the route with fewer hops
  wins, then the alphabetically smallest path.

Why search simple paths instead of running a shortest-path algorithm on
``-log(rate)``: if a loop of conversions multiplies to more than 1 (an
arbitrage cycle), "best rate" over non-simple paths is unbounded, and
Bellman-Ford/Dijkstra no longer give a meaningful answer. Restricting to simple
paths keeps the answer well defined. The cost is that the search is
exponential in the worst case, which is why hops are capped (``MAX_HOPS``).

All arithmetic uses ``Decimal``. Route rates are rounded to 12 decimal places,
and the rounded value is both shown and used, so a conversion can always be
reproduced from the response.

Registration is copy-on-write: a batch is validated in full, applied to a copy
of the book, and only then swapped in, so a failed batch leaves no trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Callable

from .errors import NotFound, ValidationFailed
from .validation import _batch_items, _check_object, _Errors

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
RATE_STR_RE = re.compile(r"^\d{1,9}(\.\d{1,12})?$")
RATE_PLACES = Decimal("1e-12")
MAX_RATE = Decimal("1000000000")
MAX_HOPS = 4
# Two route rates closer than this (relative) count as the same rate.
REL_TOLERANCE = Decimal("1e-9")
QUOTE_FIELDS = {"from", "to", "rate"}

Pair = tuple[str, str]


@dataclass(frozen=True)
class Quote:
    source: str
    target: str
    rate: Decimal


@dataclass(frozen=True)
class Route:
    path: tuple[str, ...]
    rate: Decimal

    @property
    def hops(self) -> int:
        return len(self.path) - 1

    def to_dict(self) -> dict[str, Any]:
        return {"path": list(self.path), "hops": self.hops, "rate": rate_str(self.rate)}


def rate_str(value: Decimal) -> str:
    text = format(value.quantize(RATE_PLACES).normalize(), "f")
    return text


def _product(rates: list[Decimal]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 50
        result = Decimal(1)
        for r in rates:
            result *= r
        return result.quantize(RATE_PLACES)


# --------------------------------------------------------------- validation


def check_currency(value: Any, path: str, errors: _Errors) -> str | None:
    if not isinstance(value, str) or not CURRENCY_RE.match(value):
        errors.add(path, "must be a 3-letter upper-case ISO 4217 code, e.g. 'USD'")
        return None
    return value


def _parse_rate(value: Any, path: str, errors: _Errors) -> Decimal | None:
    if isinstance(value, bool):
        errors.add(path, "must be a number or numeric string, not a boolean")
        return None
    if isinstance(value, str):
        if not RATE_STR_RE.match(value):
            errors.add(path, "must look like '83.25' (at most 12 decimal places)")
            return None
        rate = Decimal(value)
    elif isinstance(value, (Decimal, int)):
        rate = Decimal(value)
        if not rate.is_finite():
            errors.add(path, "must be finite")
            return None
        if rate != rate.quantize(RATE_PLACES):
            errors.add(path, "must have at most 12 decimal places")
            return None
    else:
        errors.add(path, "must be a number or numeric string")
        return None
    if rate <= 0:
        errors.add(path, "must be greater than 0")
        return None
    if rate > MAX_RATE:
        errors.add(path, f"must be at most {MAX_RATE}")
        return None
    return rate


def parse_rate_batch(body: Any) -> list[Quote]:
    """Validate a ``{"rates": [...]}`` batch.

    Errors are reported in a fixed order, so the same bad input always gives the
    same response: record by record, and within a record: shape, unknown
    fields, missing fields, ``from``, ``to``, same-currency, ``rate``. Duplicate
    pairs within the batch are reported on the later record.
    """
    items = _batch_items(body, "rates")
    errors = _Errors()
    quotes: list[Quote] = []
    seen: dict[Pair, int] = {}
    for i, item in enumerate(items):
        path = f"rates[{i}]"
        if not _check_object(item, path, QUOTE_FIELDS, QUOTE_FIELDS, errors):
            continue
        src = check_currency(item.get("from"), f"{path}.from", errors) if "from" in item else None
        dst = check_currency(item.get("to"), f"{path}.to", errors) if "to" in item else None
        if src is not None and src == dst:
            errors.add(f"{path}.to", "must differ from 'from'")
            dst = None
        rate = _parse_rate(item.get("rate"), f"{path}.rate", errors) if "rate" in item else None
        if src is not None and dst is not None:
            pair = (src, dst)
            if pair in seen:
                errors.add(path, f"duplicates the {src}->{dst} quote in rates[{seen[pair]}]")
                continue
            seen[pair] = i
        if src is not None and dst is not None and rate is not None:
            quotes.append(Quote(src, dst, rate))
    errors.raise_if_any()
    return quotes


# ---------------------------------------------------------------- rate book


class RateBook:
    """Not thread-safe on its own: the service calls it under its lock."""

    def __init__(self, max_hops: int = MAX_HOPS) -> None:
        if type(max_hops) is not int or max_hops < 1:
            raise ValueError("max_hops must be a positive integer")
        self.max_hops = max_hops
        # source -> {target -> rate}. Replaced wholesale, never edited in place.
        self._graph: dict[str, dict[str, Decimal]] = {}

    def apply(self, quotes: list[Quote], on_step: Callable[[int], None] | None = None) -> int:
        """Add or replace quotes. All of them land, or none do."""
        staged = {src: dict(targets) for src, targets in self._graph.items()}
        for i, q in enumerate(quotes):
            staged.setdefault(q.source, {})[q.target] = q.rate
            if on_step is not None:
                on_step(i)  # test hook: a crash here must leave the book unchanged
        self._graph = staged
        return len(quotes)

    def quotes(self) -> list[dict[str, str]]:
        return [
            {"from": src, "to": dst, "rate": rate_str(rate)}
            for src in sorted(self._graph)
            for dst, rate in sorted(self._graph[src].items())
        ]

    def direct(self, source: str, target: str) -> Route:
        self._check_pair(source, target)
        rate = self._graph.get(source, {}).get(target)
        if rate is None:
            raise NotFound(f"No {source}->{target} quote")
        return Route((source, target), rate)

    def routes(self, source: str, target: str) -> list[Route]:
        """Direct route plus every route via one intermediate currency."""
        self._check_pair(source, target)
        out: list[Route] = []
        first_legs = self._graph.get(source, {})
        if target in first_legs:
            out.append(Route((source, target), first_legs[target]))
        for mid, r1 in first_legs.items():
            if mid == target:
                continue
            r2 = self._graph.get(mid, {}).get(target)
            if r2 is not None:
                out.append(Route((source, mid, target), _product([r1, r2])))
        if not out:
            raise NotFound(f"No route from {source} to {target} with at most one intermediate")
        return sorted(out, key=lambda r: (r.rate, r.path))

    def best(self, source: str, target: str) -> Route:
        self._check_pair(source, target)
        candidates = self._simple_paths(source, target)
        if not candidates:
            raise NotFound(f"No route from {source} to {target} within {self.max_hops} conversions")
        top = max(r.rate for r in candidates)
        floor = top - top * REL_TOLERANCE
        near_best = [r for r in candidates if r.rate >= floor]
        return min(near_best, key=lambda r: (r.hops, r.path))

    def convert(self, amount: Decimal, source: str, target: str) -> tuple[Decimal, Route]:
        route = self.best(source, target)
        with localcontext() as ctx:
            ctx.prec = 50
            converted = amount * route.rate
        return converted, route

    # ---------------------------------------------------------------- private

    def _simple_paths(self, source: str, target: str) -> list[Route]:
        found: list[Route] = []
        path = [source]
        rates: list[Decimal] = []
        visited = {source}

        def walk(node: str) -> None:
            if len(rates) == self.max_hops:
                return
            for nxt in sorted(self._graph.get(node, {})):
                if nxt in visited:
                    continue
                rates.append(self._graph[node][nxt])
                path.append(nxt)
                if nxt == target:
                    found.append(Route(tuple(path), _product(rates)))
                else:
                    visited.add(nxt)
                    walk(nxt)
                    visited.discard(nxt)
                path.pop()
                rates.pop()

        walk(source)
        return found

    @staticmethod
    def _check_pair(source: Any, target: Any) -> None:
        errors = _Errors()
        check_currency(source, "from", errors)
        check_currency(target, "to", errors)
        if not errors.items and source == target:
            errors.add("to", "must differ from 'from'")
        if errors.items:
            raise ValidationFailed("Invalid currency pair", errors.items)
