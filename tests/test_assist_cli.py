"""Mapping assistant (offline), CLI behaviour and repository hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from formfill import cli
from formfill.mapping_assist import (
    AnthropicSuggester,
    HeuristicSuggester,
    OpenAISuggester,
    approve_draft,
    draft_mapping,
    parse_model_output,
    schema_from_record,
    write_draft,
)
from formfill.pdf import PdfField, inspect_fields
from formfill.registry import TemplateRegistry

REPO = Path(__file__).resolve().parent.parent


# -- mapping assistant -------------------------------------------------------------------


def test_heuristic_suggester_is_deterministic_and_finds_obvious_matches(samples, source):
    pdf = samples / "forms" / "bluepeak_sepa_mandate.pdf"
    fields = list(inspect_fields(pdf).values())
    schema = schema_from_record(source.get_client("CL-1001"))
    first = HeuristicSuggester().suggest(fields, schema)
    assert first == HeuristicSuggester().suggest(fields, schema)
    by_name = {s.pdf_field: s for s in first}
    assert by_name["iban"].source == "banking.iban"
    assert by_name["bic"].source == "banking.bic"
    assert by_name["account_reference"].source == "contract.account_reference"
    assert by_name["frequency"].type == "radio"


def test_draft_is_written_inactive_and_refused_by_registry(samples, source):
    pdf = samples / "forms" / "bluepeak_sepa_mandate.pdf"
    draft = draft_mapping(
        pdf,
        source.get_client("CL-1001"),
        HeuristicSuggester(),
        template_id="new_form",
        pdf_ref="../../forms/bluepeak_sepa_mandate.pdf",
    )
    path = write_draft(draft, samples / "mappings" / "_drafts" / "new_form.yaml")
    assert "DRAFT" in path.read_text()
    assert yaml.safe_load(path.read_text())["status"] == "draft"
    drafts = TemplateRegistry.from_directory(path.parent)
    with pytest.raises(PermissionError):
        drafts.get("new_form")
    # The main registry does not even see the drafts folder.
    assert "new_form" not in TemplateRegistry.from_directory(samples / "mappings").templates


def test_approving_a_draft_requires_a_cleaned_file(samples, source):
    pdf = samples / "forms" / "bluepeak_sepa_mandate.pdf"
    drafts_dir = samples / "mappings" / "_drafts"
    draft = draft_mapping(
        pdf,
        source.get_client("CL-1001"),
        HeuristicSuggester(),
        template_id="new_form",
        pdf_ref="../../forms/bluepeak_sepa_mandate.pdf",
    )
    path = write_draft(draft, drafts_dir / "new_form.yaml")
    with pytest.raises(ValueError, match="review markers"):
        approve_draft(path, "ops.lead", samples / "mappings")

    # A person reviews: keeps confident matches, removes markers and unmatched rows.
    data = yaml.safe_load(path.read_text())
    data["fields"] = [
        {k: v for k, v in f.items() if k not in {"confidence", "rationale", "todo"}}
        for f in data["fields"]
        if f.get("source")
    ]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    target = approve_draft(path, "ops.lead", samples / "mappings")
    registry = TemplateRegistry.from_directory(samples / "mappings")
    approved = registry.get("new_form")
    assert approved.approved_by == "ops.lead" and approved.pdf_path.exists()
    assert target.name == "new_form.yaml"


def test_model_output_is_sanitised_against_the_schema():
    fields = [PdfField("iban", "text"), PdfField("agree", "checkbox"), PdfField("x", "text")]
    text = "Here you go:\n" + json.dumps(
        [
            {"pdf_field": "iban", "source": "banking.iban", "confidence": 0.97},
            {"pdf_field": "agree", "source": "made.up.path", "confidence": 0.9},
            {"pdf_field": "ghost", "source": "banking.iban", "confidence": 1},
        ]
    )
    result = {s.pdf_field: s for s in parse_model_output(text, fields, ["banking.iban"])}
    assert result["iban"].source == "banking.iban" and result["iban"].confidence == 0.97
    assert result["agree"].source is None and "unknown path" in result["agree"].rationale
    assert result["x"].source is None
    assert "ghost" not in result


def test_llm_adapters_send_field_names_and_paths_but_never_values(source):
    record = source.get_client("CL-1001")
    schema = schema_from_record(record)
    fields = [PdfField("debtor_iban", "text")]
    sent: list[httpx.Request] = []
    reply = json.dumps([{"pdf_field": "debtor_iban", "source": "banking.iban", "confidence": 0.9}])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if "anthropic" in request.url.host:
            return httpx.Response(200, json={"content": [{"type": "text", "text": reply}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    transport = httpx.MockTransport(handler)
    for cls in (AnthropicSuggester, OpenAISuggester):
        (suggestion,) = cls(api_key="test-key", transport=transport).suggest(fields, schema)
        assert suggestion.source == "banking.iban"
    for request in sent:
        body = request.content.decode()
        assert "banking.iban" in body
        for value in ("DE52000000000012345678", "Alex", "XA1234567"):
            assert value not in body


def test_llm_adapter_requires_an_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicSuggester()


# -- CLI ---------------------------------------------------------------------------------


def _run(samples: Path, tmp_path: Path, *args: str) -> int:
    base = ["--mappings", str(samples / "mappings"),
            "--data", str(samples / "clients" / "clients.json"),
            "--workdir", str(tmp_path / "work")]  # fmt: skip
    return cli.main([*base, *args])


def test_cli_list_and_validate_exit_codes(samples, tmp_path, capsys):
    assert _run(samples, tmp_path, "list-templates") == 0
    assert "northwind_beneficiary_change" in capsys.readouterr().out
    ok = _run(
        samples,
        tmp_path,
        "validate",
        "--template",
        "bluepeak_sepa_mandate",
        "--client",
        "CL-1001",
        "--today",
        "2026-11-22",
    )
    assert ok == 0
    blocked = _run(
        samples,
        tmp_path,
        "validate",
        "--template",
        "bluepeak_sepa_mandate",
        "--client",
        "CL-1002",
        "--today",
        "2026-11-22",
    )
    assert blocked == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_cli_validate_json_output(samples, tmp_path, capsys):
    _run(
        samples,
        tmp_path,
        "validate",
        "--template",
        "northwind_beneficiary_change",
        "--client",
        "CL-1003",
        "--today",
        "2026-11-22",
        "--json",
    )
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["doubtful"] == 2


def test_cli_fill_creates_a_pending_package_and_audit_verifies(samples, tmp_path, capsys):
    code = _run(
        samples,
        tmp_path,
        "fill",
        "--template",
        "harborline_tax_residence",
        "--client",
        "CL-1001",
        "--prepared-by",
        "pat.preparer",
    )
    assert code == 0
    assert "status pending" in capsys.readouterr().out
    assert len(list((tmp_path / "work" / "packages").iterdir())) == 1
    assert _run(samples, tmp_path, "verify-audit") == 0


def test_cli_suggest_then_approve_mapping(samples, tmp_path, capsys):
    pdf = samples / "forms" / "bluepeak_sepa_mandate.pdf"
    out = samples / "mappings" / "_drafts" / "copy.yaml"
    assert (
        _run(
            samples,
            tmp_path,
            "suggest-mapping",
            str(pdf),
            "--template-id",
            "copy_form",
            "--out",
            str(out),
        )
        == 0
    )
    assert "NOT active" in capsys.readouterr().out
    # Unreviewed draft is refused.
    assert _run(samples, tmp_path, "approve-mapping", str(out), "--by", "ops.lead") == 2
    assert "review markers" in capsys.readouterr().err


def test_cli_reports_unknown_template_cleanly(samples, tmp_path, capsys):
    assert _run(samples, tmp_path, "validate", "--template", "nope") == 2
    assert "unknown template" in capsys.readouterr().err


# -- hygiene -----------------------------------------------------------------------------


def test_repository_text_contains_no_em_dash():
    em_dash = chr(0x2014)
    suffixes = {".py", ".md", ".yaml", ".yml", ".toml", ".html", ".json", ".csv", ".txt", ""}
    skip = {".git", ".venv", "work", "__pycache__", ".pytest_cache", ".ruff_cache"}
    offenders = []
    for path in REPO.rglob("*"):
        if any(part in skip or part.endswith(".egg-info") for part in path.parts):
            continue
        is_text = path.is_file() and path.suffix in suffixes
        if is_text and em_dash in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(REPO)))
    assert offenders == []
