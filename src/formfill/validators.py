"""Field validators and cross-field rules.

Each validator returns ``None`` when the value passes, or an :class:`Issue`. Severity decides
what happens next:

* ``error``: the field is INVALID and the package cannot be approved until the data is fixed.
* ``warning``: the field is DOUBTFUL. A reviewer may still approve, with the warning on record.

National-ID patterns here are deliberately labelled placeholders. Real schemes differ by
country and must be supplied by the firm's compliance team, not guessed by a library.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import conditions
from .paths import MISSING, resolve
from .transforms import TransformError, parse_date


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "error"  # "error" | "warning"
    fields: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ValidationContext:
    today: date


ValidatorFn = Callable[..., Issue | None]
_REGISTRY: dict[str, tuple[ValidatorFn, str]] = {}  # name -> (fn, default target)


def validator(name: str, target: str = "source") -> Callable[[ValidatorFn], ValidatorFn]:
    def decorator(fn: ValidatorFn) -> ValidatorFn:
        _REGISTRY[name] = (fn, target)
        return fn

    return decorator


def available() -> list[str]:
    return sorted(_REGISTRY)


# IBAN lengths for a representative subset of SEPA countries. Unknown countries are still
# checked with mod-97 but produce a warning, so the reviewer looks twice.
IBAN_LENGTHS = {
    "AT": 20, "BE": 16, "CH": 21, "CY": 28, "DE": 22, "DK": 18, "EE": 20, "ES": 24,
    "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "IE": 22, "IT": 27, "LT": 20,
    "LU": 20, "LV": 21, "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "SE": 24,
    "SI": 19, "SK": 24,
}  # fmt: skip


def iban_is_valid(raw: str) -> bool:
    iban = raw.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def iban_check_digits(country: str, bban: str) -> str:
    """Compute the two check digits for ``country`` + ``bban``. Used to build sample data."""
    rearranged = (bban + country + "00").upper()
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return f"{98 - int(digits) % 97:02d}"


@validator("iban")
def _iban(value: Any, ctx: ValidationContext, severity: str = "error") -> Issue | None:
    text = str(value).replace(" ", "").upper()
    if not iban_is_valid(text):
        return Issue("iban.checksum", "IBAN fails format or mod-97 checksum", severity)
    expected = IBAN_LENGTHS.get(text[:2])
    if expected is None:
        return Issue("iban.country", f"IBAN country {text[:2]} not in known list", "warning")
    if len(text) != expected:
        return Issue("iban.length", f"IBAN for {text[:2]} must be {expected} chars", severity)
    return None


@validator("date")
def _date(
    value: Any,
    ctx: ValidationContext,
    format: str | None = None,
    not_future: bool = False,
    severity: str = "error",
) -> Issue | None:
    try:
        parsed = parse_date(value, format)
    except TransformError:
        expected = format or "ISO 8601 (YYYY-MM-DD)"
        return Issue("date.format", f"date {value!r} does not match {expected}", severity)
    if not_future and parsed > ctx.today:
        return Issue("date.future", f"date {parsed.isoformat()} is in the future", severity)
    return None


@validator("max_age_days")
def _max_age(
    value: Any, ctx: ValidationContext, days: int, severity: str = "warning"
) -> Issue | None:
    try:
        parsed = parse_date(value)
    except TransformError:
        return Issue("date.format", f"date {value!r} is not ISO 8601", "error")
    age = (ctx.today - parsed).days
    if age > days:
        return Issue("date.stale", f"{age} days old, limit is {days}", severity)
    return None


@validator("regex")
def _regex(
    value: Any, ctx: ValidationContext, pattern: str, severity: str = "error", message: str = ""
) -> Issue | None:
    if not re.fullmatch(pattern, str(value)):
        return Issue("regex", message or f"value does not match {pattern}", severity)
    return None


# Placeholder schemes. They show the mechanism; they are not any country's real format.
NATIONAL_ID_SCHEMES = {
    "placeholder_alpha2_digits7": r"[A-Z]{2}\d{7}",
    "placeholder_digits9": r"\d{9}",
    "placeholder_digits7_letter": r"\d{7}[A-Z]",
}


@validator("national_id")
def _national_id(
    value: Any, ctx: ValidationContext, scheme: str, severity: str = "warning"
) -> Issue | None:
    pattern = NATIONAL_ID_SCHEMES.get(scheme)
    if pattern is None:
        return Issue("national_id.scheme", f"unknown national-ID scheme {scheme!r}", "error")
    if not re.fullmatch(pattern, str(value).replace(" ", "").upper()):
        return Issue("national_id.format", f"does not match scheme {scheme}", severity)
    return None


@validator("email")
def _email(value: Any, ctx: ValidationContext, severity: str = "error") -> Issue | None:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", str(value)):
        return Issue("email", "not a valid e-mail address", severity)
    return None


@validator("one_of")
def _one_of(
    value: Any,
    ctx: ValidationContext,
    values: list[Any],
    severity: str = "error",
    message: str = "",
) -> Issue | None:
    if value not in values:
        return Issue("one_of", message or f"{value!r} is not one of {values}", severity)
    return None


@validator("range")
def _range(
    value: Any,
    ctx: ValidationContext,
    min: float | None = None,
    max: float | None = None,
    severity: str = "error",
) -> Issue | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return Issue("range.type", f"{value!r} is not a number", severity)
    if min is not None and number < min:
        return Issue("range.min", f"{number:g} is below the minimum of {min:g}", severity)
    if max is not None and number > max:
        return Issue("range.max", f"{number:g} is above the maximum of {max:g}", severity)
    return None


@validator("max_length", target="output")
def _max_length(
    value: Any, ctx: ValidationContext, length: int, severity: str = "warning"
) -> Issue | None:
    if len(str(value)) > length:
        return Issue("max_length", f"{len(str(value))} chars, box holds {length}", severity)
    return None


@dataclass(frozen=True)
class ValidatorSpec:
    name: str
    kwargs: dict[str, Any]
    target: str  # "source" | "output"

    def run(self, source_value: Any, output_value: Any, ctx: ValidationContext) -> Issue | None:
        fn, _ = _REGISTRY[self.name]
        value = source_value if self.target == "source" else output_value
        return fn(value, ctx, **self.kwargs)


_SCALAR_ARG = {"max_length": "length", "max_age_days": "days", "regex": "pattern",
               "national_id": "scheme", "one_of": "values"}  # fmt: skip


def parse_spec(spec: Any) -> list[ValidatorSpec]:
    if spec is None:
        return []
    items = spec if isinstance(spec, list) else [spec]
    parsed: list[ValidatorSpec] = []
    for item in items:
        if isinstance(item, str):
            name, kwargs = item, {}
        elif isinstance(item, dict) and len(item) == 1:
            name, raw = next(iter(item.items()))
            if isinstance(raw, dict):
                kwargs = dict(raw)
            elif name in _SCALAR_ARG:
                kwargs = {_SCALAR_ARG[name]: raw}
            else:
                raise ValueError(f"validator {name!r} needs a mapping of arguments")
        else:
            raise ValueError(f"invalid validator spec: {item!r}")
        if name not in _REGISTRY:
            raise ValueError(f"unknown validator {name!r}; available: {', '.join(available())}")
        target = kwargs.pop("on", _REGISTRY[name][1])
        if target not in {"source", "output"}:
            raise ValueError(f"validator 'on' must be source or output, got {target!r}")
        parsed.append(ValidatorSpec(name, kwargs, target))
    return parsed


# ---------------------------------------------------------------------------
# Cross-field rules: checks that involve more than one value in the record.
# ---------------------------------------------------------------------------

_RULE_TYPES = {"sum_equals", "date_order", "required_if"}


def check_rule_spec(rule: dict[str, Any]) -> None:
    kind = rule.get("type")
    if kind not in _RULE_TYPES:
        raise ValueError(f"unknown rule type {kind!r}; expected one of {sorted(_RULE_TYPES)}")
    if kind == "sum_equals" and not (rule.get("paths") and "total" in rule):
        raise ValueError("sum_equals needs 'paths' and 'total'")
    if kind == "date_order" and not (rule.get("before") and rule.get("after")):
        raise ValueError("date_order needs 'before' and 'after'")
    if kind == "required_if":
        if not rule.get("path") or "when" not in rule:
            raise ValueError("required_if needs 'path' and 'when'")
        conditions.check_spec(rule["when"])


def run_rule(rule: dict[str, Any], record: Any) -> Issue | None:
    kind = rule["type"]
    severity = rule.get("severity", "error")
    name = rule.get("name", kind)
    if kind == "sum_equals":
        values = [resolve(record, p) for p in rule["paths"]]
        present = [v for v in values if v is not MISSING and v is not None]
        if not present:
            return None
        try:
            total = sum(float(v) for v in present)
        except (TypeError, ValueError):
            return Issue(name, "non-numeric value in sum", severity, tuple(rule["paths"]))
        if abs(total - float(rule["total"])) > 1e-9:
            msg = rule.get("message") or f"sum is {total:g}, expected {rule['total']}"
            return Issue(name, msg, severity, tuple(rule["paths"]))
        return None
    if kind == "date_order":
        first, second = resolve(record, rule["before"]), resolve(record, rule["after"])
        if first is MISSING or second is MISSING:
            return None
        try:
            if parse_date(first) > parse_date(second):
                msg = rule.get("message") or f"{rule['before']} must not be after {rule['after']}"
                return Issue(name, msg, severity, (rule["before"], rule["after"]))
        except TransformError:
            return Issue(
                name, "unparseable date in rule", severity, (rule["before"], rule["after"])
            )
        return None
    # required_if
    if conditions.evaluate(rule["when"], record):
        value = resolve(record, rule["path"])
        if value is MISSING or value in (None, ""):
            msg = rule.get("message") or f"{rule['path']} is required here"
            return Issue(name, msg, severity, (rule["path"],))
    return None
