"""Named value transforms referenced from mapping YAML.

A mapping declares transforms as a list. Each item is either a bare name (``upper``) or a
single-key mapping with arguments (``{date: {to: "%d/%m/%Y"}}``). Transforms are pure
functions: same input, same output, no I/O. That keeps a filled form reproducible.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

TransformFn = Callable[..., Any]


class TransformError(ValueError):
    """Raised when a value cannot be transformed. The field is reported as invalid."""


_REGISTRY: dict[str, TransformFn] = {}


def transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def decorator(fn: TransformFn) -> TransformFn:
        _REGISTRY[name] = fn
        return fn

    return decorator


def available() -> list[str]:
    return sorted(_REGISTRY)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


@transform("strip")
def _strip(value: Any) -> str:
    return _text(value).strip()


@transform("upper")
def _upper(value: Any) -> str:
    return _text(value).upper()


@transform("lower")
def _lower(value: Any) -> str:
    return _text(value).lower()


@transform("title")
def _title(value: Any) -> str:
    return _text(value).title()


@transform("digits_only")
def _digits_only(value: Any) -> str:
    return "".join(ch for ch in _text(value) if ch.isdigit())


@transform("iban_spaced")
def _iban_spaced(value: Any) -> str:
    compact = _text(value).replace(" ", "").upper()
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


def parse_date(value: Any, fmt: str | None = None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value).strip()
    try:
        if fmt:
            return datetime.strptime(text, fmt).date()
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise TransformError(f"not a valid date: {text!r}") from exc


@transform("date")
def _date(value: Any, to: str = "%d/%m/%Y", **kwargs: Any) -> str:
    source_format = kwargs.get("from")
    return parse_date(value, source_format).strftime(to)


@transform("amount")
def _amount(value: Any, places: int = 2, decimal: str = ".", thousands: str = "") -> str:
    try:
        number = Decimal(_text(value).replace(",", ".").replace(" ", ""))
    except InvalidOperation as exc:
        raise TransformError(f"not a number: {value!r}") from exc
    quantum = Decimal(1).scaleb(-places)
    rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
    integer, _, fraction = f"{rounded:f}".partition(".")
    sign = "-" if integer.startswith("-") else ""
    integer = integer.lstrip("-")
    groups = []
    while len(integer) > 3:
        groups.insert(0, integer[-3:])
        integer = integer[:-3]
    groups.insert(0, integer)
    body = thousands.join(groups)
    return f"{sign}{body}{decimal}{fraction}" if places > 0 else f"{sign}{body}"


@transform("map")
def _map(value: Any, values: dict[str, Any], default: Any = None) -> Any:
    key = _text(value)
    if key in values:
        return values[key]
    if default is None:
        raise TransformError(f"no mapping for value {key!r}")
    return default


@transform("equals")
def _equals(value: Any, **kwargs: Any) -> bool:
    """``{equals: life}`` turns a value into a boolean, typically to drive a checkbox."""
    return _text(value) == _text(kwargs.get("value"))


@transform("join")
def _join(value: Any, sep: str = " ") -> str:
    if isinstance(value, (list, tuple)):
        return sep.join(_text(v).strip() for v in value if _text(v).strip())
    return _text(value)


@transform("truncate")
def _truncate(value: Any, length: int) -> str:
    return _text(value)[:length]


def parse_spec(spec: Any) -> list[tuple[str, dict[str, Any]]]:
    """Normalise a YAML transform spec into ``[(name, kwargs), ...]``."""
    if spec is None:
        return []
    items = spec if isinstance(spec, list) else [spec]
    parsed: list[tuple[str, dict[str, Any]]] = []
    for item in items:
        if isinstance(item, str):
            name, kwargs = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, raw = next(iter(item.items()))
            kwargs = raw if isinstance(raw, dict) else {"value": raw}
        else:
            raise ValueError(f"invalid transform spec: {item!r}")
        if name not in _REGISTRY:
            raise ValueError(f"unknown transform {name!r}; available: {', '.join(available())}")
        parsed.append((name, dict(kwargs)))
    return parsed


def apply(value: Any, spec: list[tuple[str, dict[str, Any]]]) -> Any:
    for name, kwargs in spec:
        value = _REGISTRY[name](value, **kwargs)
    return value
