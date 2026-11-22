"""Paths, transforms, conditions and validators: the pure building blocks."""

from __future__ import annotations

from datetime import date

import pytest

from formfill import conditions, transforms, validators
from formfill.paths import MISSING, iter_leaf_paths, parse_path, resolve
from formfill.validators import ValidationContext, iban_check_digits, iban_is_valid

CTX = ValidationContext(today=date(2026, 11, 22))
RECORD = {
    "person": {"name": "Alex", "nickname": None},
    "items": [{"share": 60}, {"share": 40}],
    "flag": False,
}


# -- paths -------------------------------------------------------------------------------


def test_parse_path_handles_dots_and_indexes():
    assert parse_path("items[1].share") == ["items", 1, "share"]
    with pytest.raises(ValueError):
        parse_path("")


def test_resolve_distinguishes_missing_from_null():
    assert resolve(RECORD, "items[0].share") == 60
    assert resolve(RECORD, "person.nickname") is None
    assert resolve(RECORD, "person.unknown") is MISSING
    assert resolve(RECORD, "items[5].share") is MISSING
    assert resolve(RECORD, "flag") is False


def test_iter_leaf_paths_lists_schema_without_values():
    paths = set(iter_leaf_paths(RECORD))
    assert {"person.name", "items[0].share", "items[1].share", "flag"} <= paths


# -- transforms --------------------------------------------------------------------------


def test_date_transform_formats_iso_input():
    spec = transforms.parse_spec([{"date": {"to": "%d/%m/%Y"}}])
    assert transforms.apply("1981-04-12", spec) == "12/04/1981"
    with pytest.raises(transforms.TransformError):
        transforms.apply("12th April", spec)


def test_amount_transform_rounds_and_groups():
    spec = transforms.parse_spec([{"amount": {"decimal": ",", "thousands": "."}}])
    assert transforms.apply(250000, spec) == "250.000,00"
    assert transforms.apply("1234.565", transforms.parse_spec(["amount"])) == "1234.57"


def test_map_transform_fails_loudly_on_unknown_code():
    spec = transforms.parse_spec([{"map": {"values": {"DE": "Germany"}}}])
    assert transforms.apply("DE", spec) == "Germany"
    with pytest.raises(transforms.TransformError):
        transforms.apply("XX", spec)


def test_transform_chain_join_then_upper_and_iban_spacing():
    spec = transforms.parse_spec(["join", "upper"])
    assert transforms.apply(["Alex", "", "Example"], spec) == "ALEX EXAMPLE"
    iban = transforms.apply("de52000000000012345678", transforms.parse_spec(["iban_spaced"]))
    assert iban == "DE52 0000 0000 0012 3456 78"


def test_unknown_transform_is_rejected_at_parse_time():
    with pytest.raises(ValueError, match="unknown transform"):
        transforms.parse_spec(["reverse"])


# -- conditions --------------------------------------------------------------------------


def test_conditions_cover_all_any_not_and_exists():
    record = {"c": {"type": "life", "amount": 10}, "tin": ""}
    assert conditions.evaluate({"path": "c.type", "equals": "life"}, record)
    assert conditions.evaluate(
        {"all": [{"path": "c.amount", "gt": 5}, {"not": {"path": "tin", "exists": True}}]}, record
    )
    assert conditions.evaluate(
        {"any": [{"path": "c.type", "in": ["savings"]}, {"path": "c.amount", "lt": 20}]}, record
    )
    assert conditions.evaluate({"path": "absent", "not_equals": "x"}, record)
    assert not conditions.evaluate({"path": "absent", "equals": "x"}, record)


def test_condition_spec_typos_fail_fast():
    with pytest.raises(conditions.ConditionError):
        conditions.check_spec({"path": "a", "equal": 1})
    with pytest.raises(conditions.ConditionError):
        conditions.check_spec({"all": []})


# -- validators --------------------------------------------------------------------------


def test_iban_checksum_and_generated_check_digits():
    bban = "000000000012345678"
    iban = "DE" + iban_check_digits("DE", bban) + bban
    assert iban_is_valid(iban)
    assert not iban_is_valid(iban[:-1] + "9")
    (spec,) = validators.parse_spec(["iban"])
    assert spec.run(iban, None, CTX) is None
    assert spec.run(iban[:-1] + "9", None, CTX).code == "iban.checksum"


def test_iban_wrong_length_for_country_is_an_error():
    bban = "0000000000123456789"  # one digit too long for DE
    iban = "DE" + iban_check_digits("DE", bban) + bban
    (spec,) = validators.parse_spec(["iban"])
    issue = spec.run(iban, None, CTX)
    assert issue.code == "iban.length" and issue.severity == "error"


def test_date_validators_future_and_staleness():
    (future,) = validators.parse_spec([{"date": {"not_future": True}}])
    assert future.run("2027-01-01", None, CTX).code == "date.future"
    assert future.run("2026-11-22", None, CTX) is None
    (stale,) = validators.parse_spec([{"max_age_days": 365}])
    issue = stale.run("2024-03-10", None, CTX)
    assert issue.code == "date.stale" and issue.severity == "warning"


def test_national_id_placeholder_scheme_warns_rather_than_blocks():
    (spec,) = validators.parse_spec([{"national_id": {"scheme": "placeholder_alpha2_digits7"}}])
    assert spec.run("xa1234567", None, CTX) is None
    assert spec.run("12345", None, CTX).severity == "warning"


def test_max_length_checks_output_not_source():
    (spec,) = validators.parse_spec([{"max_length": 5}])
    assert spec.target == "output"
    assert spec.run("short", "much longer", CTX).code == "max_length"


def test_cross_field_rules():
    record = {"b": [{"s": 50}, {"s": 40}], "d1": "2020-01-01", "d2": "2019-01-01", "us": True}
    total = {"type": "sum_equals", "paths": ["b[0].s", "b[1].s"], "total": 100}
    assert validators.run_rule(total, record).fields == ("b[0].s", "b[1].s")
    order = {"type": "date_order", "before": "d1", "after": "d2"}
    assert validators.run_rule(order, record) is not None
    req = {"type": "required_if", "path": "us_tin", "when": {"path": "us", "equals": True}}
    assert validators.run_rule(req, record).fields == ("us_tin",)
    assert validators.run_rule(req, {**record, "us_tin": "123-45-6789"}) is None
    with pytest.raises(ValueError):
        validators.check_rule_spec({"type": "sum_equals", "paths": ["a"]})
