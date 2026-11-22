"""PDF AcroForm handling and the fill engine end to end."""

from __future__ import annotations

from dataclasses import replace

import pytest
from pypdf import PdfReader

from formfill.engine import BLOCKING, Status, check_mapping, prepare, render, unmapped_fields
from formfill.pdf import (
    PdfFormError,
    fill_pdf,
    flatten_pdf,
    inspect_fields,
    read_values,
    sha256_file,
)
from formfill.registry import FieldMapping


def _by_field(report):
    return {f.pdf_field: f for f in report.fields}


# -- pdf ---------------------------------------------------------------------------------


def test_sample_forms_expose_text_checkbox_and_radio_fields(samples):
    fields = inspect_fields(samples / "forms" / "northwind_beneficiary_change.pdf")
    assert fields["policy_number"].kind == "text"
    assert fields["policy_number"].max_len == 12
    assert fields["irrevocable"].kind == "checkbox" and fields["irrevocable"].on_state == "Yes"
    assert fields["contract_type"].kind == "radio"
    assert fields["contract_type"].states == ("life", "savings")


def test_fill_writes_text_checkbox_and_radio_and_leaves_template_untouched(samples, tmp_path):
    template = samples / "forms" / "northwind_beneficiary_change.pdf"
    before = sha256_file(template)
    out = fill_pdf(
        template,
        {"holder_name": "Alex Example", "irrevocable": True, "contract_type": "savings"},
        tmp_path / "out.pdf",
    )
    values = read_values(out)
    assert values["holder_name"] == "Alex Example"
    assert values["irrevocable"] == "Yes"
    assert values["contract_type"] == "savings"
    assert sha256_file(template) == before


def test_fill_rejects_unknown_fields_and_unknown_radio_options(samples, tmp_path):
    template = samples / "forms" / "northwind_beneficiary_change.pdf"
    with pytest.raises(PdfFormError, match="not in"):
        fill_pdf(template, {"renamed_field": "x"}, tmp_path / "a.pdf")
    with pytest.raises(PdfFormError, match="no option"):
        fill_pdf(template, {"contract_type": "annuity"}, tmp_path / "b.pdf")


def test_flatten_removes_form_fields_but_keeps_pages(samples, tmp_path):
    template = samples / "forms" / "harborline_tax_residence.pdf"
    filled = fill_pdf(
        template, {"full_name": "Alex Example", "us_person": True}, tmp_path / "f.pdf"
    )
    flat = flatten_pdf(filled, tmp_path / "flat.pdf")
    reader = PdfReader(str(flat))
    assert not reader.get_fields()
    assert len(reader.pages) == 1
    assert "Alex Example" in reader.pages[0].extract_text()


# -- engine ------------------------------------------------------------------------------


def test_clean_client_fills_every_template_without_blocking(registry, source, today):
    record = source.get_client("CL-1001")
    for mapping in registry.list():
        report = prepare(mapping, record, "CL-1001", today=today)
        assert not report.blocking, mapping.template_id
        assert report.count(Status.DOUBTFUL) == 0


def test_conditional_field_is_skipped_when_condition_is_false(registry, source, today):
    mapping = registry.get("northwind_beneficiary_change")
    fields = _by_field(prepare(mapping, source.get_client("CL-1002"), "CL-1002", today=today))
    assert fields["life_cover_amount"].status == Status.SKIPPED
    assert (
        "life_cover_amount"
        not in prepare(mapping, source.get_client("CL-1002"), "CL-1002", today=today).pdf_values()
    )


def test_cross_field_rule_blocks_and_marks_the_right_rows(registry, source, today):
    mapping = registry.get("northwind_beneficiary_change")
    report = prepare(mapping, source.get_client("CL-1002"), "CL-1002", today=today)
    assert report.blocking
    fields = _by_field(report)
    assert fields["benef1_share"].status == Status.INVALID
    assert fields["benef2_share"].status == Status.INVALID
    assert [i.code for i in report.rule_issues] == ["shares_total_100"]


def test_warnings_make_fields_doubtful_but_not_blocking(registry, source, today):
    mapping = registry.get("northwind_beneficiary_change")
    report = prepare(mapping, source.get_client("CL-1003"), "CL-1003", today=today)
    fields = _by_field(report)
    assert fields["kyc_review_date"].status == Status.DOUBTFUL
    assert fields["holder_national_id"].status == Status.DOUBTFUL
    assert fields["holder_email"].status == Status.EMPTY
    assert not report.blocking
    assert "kyc_review_date" in report.pdf_values()  # doubtful values are still written


def test_invalid_iban_is_never_written_to_the_pdf(registry, source, today):
    mapping = registry.get("bluepeak_sepa_mandate")
    report = prepare(mapping, source.get_client("CL-1002"), "CL-1002", today=today)
    fields = _by_field(report)
    assert fields["iban"].status == Status.INVALID
    assert "iban" not in report.pdf_values()
    assert fields["amount"].status == Status.DOUBTFUL  # below provider minimum: a warning
    assert report.blocking


def test_missing_required_value_and_required_if_rule(registry, source, today):
    mapping = registry.get("harborline_tax_residence")
    record = source.get_client("CL-1003")
    report = prepare(mapping, record, "CL-1003", today=today)
    assert _by_field(report)["us_tin"].status == Status.MISSING
    del record["contract"]["member_number"]
    report = prepare(mapping, record, "CL-1003", today=today)
    assert _by_field(report)["member_number"].status == Status.MISSING
    assert {f.status for f in report.fields} & BLOCKING


def test_text_longer_than_pdf_maxlen_is_flagged(registry, source, today):
    mapping = registry.get("northwind_beneficiary_change")
    record = source.get_client("CL-1001")
    record["person"]["first_name"] = "Alexandra-Maximiliana-Josephine-Emerald"
    report = prepare(mapping, record, "CL-1001", today=today)
    issue_codes = [i.code for i in _by_field(report)["holder_name"].issues]
    assert "pdf.max_len" in issue_codes


def test_render_round_trips_values_into_the_pdf(registry, source, today, tmp_path):
    mapping = registry.get("northwind_beneficiary_change")
    report = prepare(mapping, source.get_client("CL-1001"), "CL-1001", today=today)
    values = read_values(render(mapping, report, tmp_path / "filled.pdf"))
    assert values["policy_number"] == "NWA-00012345"
    assert values["life_cover_amount"] == "250.000,00"
    assert values["contract_type"] == "life"
    assert values["irrevocable"] == "Off"
    assert values["sign_date"] == ""  # left for the signer


def test_check_mapping_catches_drift_between_yaml_and_pdf(registry):
    mapping = registry.get("harborline_tax_residence")
    assert check_mapping(mapping) == []
    assert unmapped_fields(mapping) == ["declaration_accepted"]
    drifted = replace(mapping, fields=(*mapping.fields, FieldMapping("old_field", source="x")))
    assert check_mapping(drifted) == ["old_field: not a field in the PDF"]
