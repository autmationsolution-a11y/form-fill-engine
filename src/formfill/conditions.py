"""Declarative conditions for mapping YAML. No ``eval``: only a fixed set of operators.

Examples::

    when: {path: contract.contract_type, equals: life}
    when: {all: [{path: tax.us_person, equals: false}, {path: tax.tin, exists: true}]}
    when: {any: [...]}
    when: {not: {path: banking.iban, exists: true}}
"""

from __future__ import annotations

from typing import Any

from .paths import MISSING, resolve

_OPERATORS = {"equals", "not_equals", "in", "not_in", "exists", "gt", "lt"}


class ConditionError(ValueError):
    pass


def check_spec(spec: Any) -> None:
    """Validate a condition spec at load time so a typo fails fast, not mid-batch."""
    if not isinstance(spec, dict) or not spec:
        raise ConditionError(f"condition must be a non-empty mapping, got {spec!r}")
    if "all" in spec or "any" in spec:
        key = "all" if "all" in spec else "any"
        items = spec[key]
        if not isinstance(items, list) or not items:
            raise ConditionError(f"'{key}' needs a non-empty list")
        for item in items:
            check_spec(item)
        return
    if "not" in spec:
        check_spec(spec["not"])
        return
    if "path" not in spec:
        raise ConditionError(f"condition needs 'path': {spec!r}")
    ops = set(spec) - {"path"}
    if len(ops) != 1 or not ops <= _OPERATORS:
        raise ConditionError(
            f"condition needs exactly one operator from {sorted(_OPERATORS)}: {spec!r}"
        )


def evaluate(spec: dict[str, Any], record: Any) -> bool:
    if "all" in spec:
        return all(evaluate(item, record) for item in spec["all"])
    if "any" in spec:
        return any(evaluate(item, record) for item in spec["any"])
    if "not" in spec:
        return not evaluate(spec["not"], record)

    value = resolve(record, spec["path"])
    (op,) = set(spec) - {"path"}
    expected = spec[op]
    if op == "exists":
        present = value is not MISSING and value not in (None, "")
        return present is bool(expected)
    if value is MISSING:
        return op in {"not_equals", "not_in"}
    if op == "equals":
        return value == expected
    if op == "not_equals":
        return value != expected
    if op == "in":
        return value in expected
    if op == "not_in":
        return value not in expected
    try:
        if op == "gt":
            return float(value) > float(expected)
        return float(value) < float(expected)
    except (TypeError, ValueError):
        return False
