"""Template registry loading rules and read-only data source adapters."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from formfill.adapters import (
    ClientNotFound,
    CsvFileSource,
    JsonFileSource,
    OAuth2ClientCredentials,
    ReadOnlyViolation,
    RestCrmSource,
)
from formfill.engine import prepare
from formfill.registry import MappingError, TemplateRegistry, load_mapping

MINIMAL = """
template_id: t1
pdf: form.pdf
status: {status}
approved_by: {approver}
fields:
  - pdf_field: a
    source: person.name
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


# -- registry ----------------------------------------------------------------------------


def test_registry_loads_the_three_sample_templates(registry):
    assert registry.errors == {}
    assert [t.template_id for t in registry.list()] == [
        "bluepeak_sepa_mandate",
        "harborline_tax_residence",
        "northwind_beneficiary_change",
    ]
    assert all(t.is_approved and t.pdf_path.exists() for t in registry.list())


def test_draft_mapping_cannot_be_used_to_fill(tmp_path):
    _write(tmp_path, "t1.yaml", MINIMAL.format(status="draft", approver="null"))
    registry = TemplateRegistry.from_directory(tmp_path)
    assert "t1" in registry.templates
    with pytest.raises(PermissionError, match="approve"):
        registry.get("t1")


def test_approved_status_without_named_approver_is_still_refused(tmp_path):
    _write(tmp_path, "t1.yaml", MINIMAL.format(status="approved", approver="null"))
    with pytest.raises(PermissionError):
        TemplateRegistry.from_directory(tmp_path).get("t1")


def test_malformed_mapping_is_reported_with_file_and_field(tmp_path):
    bad = MINIMAL.format(status="approved", approver="x") + "    validate: [no_such_check]\n"
    _write(tmp_path, "bad.yaml", bad)
    registry = TemplateRegistry.from_directory(tmp_path)
    assert registry.templates == {}
    assert "bad.yaml" in registry.errors
    assert (
        "'a'" in registry.errors["bad.yaml"] and "unknown validator" in registry.errors["bad.yaml"]
    )


def test_field_needs_exactly_one_value_origin(tmp_path):
    text = MINIMAL.format(status="approved", approver="x") + "    value: constant\n"
    path = _write(tmp_path, "t1.yaml", text)
    with pytest.raises(MappingError, match="exactly one"):
        load_mapping(path)


def test_duplicate_pdf_fields_are_rejected(tmp_path):
    text = MINIMAL.format(status="approved", approver="x") + "  - pdf_field: a\n    value: 1\n"
    with pytest.raises(MappingError, match="duplicate"):
        load_mapping(_write(tmp_path, "t1.yaml", text))


# -- file adapters -----------------------------------------------------------------------


def test_json_source_returns_copies_so_callers_cannot_mutate_it(source):
    record = source.get_client("CL-1001")
    record["person"]["first_name"] = "Changed"
    assert source.get_client("CL-1001")["person"]["first_name"] == "Alex"
    assert source.read_only is True
    assert not hasattr(source, "update_client")
    with pytest.raises(ClientNotFound):
        source.get_client("CL-9999")


def test_csv_source_keeps_text_like_values_as_text(samples):
    record = CsvFileSource(samples / "clients" / "clients.csv").get_client("CL-1001")
    assert record["address"]["postcode"] == "01234"  # leading zero survives
    assert record["banking"]["mandate_consent"] is True
    assert record["beneficiaries"][1]["share"] == 40
    assert (
        "email"
        not in CsvFileSource(samples / "clients" / "clients.csv").get_client("CL-1003")["person"]
    )


def test_csv_and_json_exports_produce_identical_reports(samples, registry, today):
    json_src = JsonFileSource(samples / "clients" / "clients.json")
    csv_src = CsvFileSource(samples / "clients" / "clients.csv")
    for mapping in registry.list():
        for cid in json_src.list_clients():
            a = prepare(mapping, json_src.get_client(cid), cid, today=today).to_dict()
            b = prepare(mapping, csv_src.get_client(cid), cid, today=today).to_dict()
            assert a == b, (mapping.template_id, cid)


# -- REST adapter ------------------------------------------------------------------------


def _crm_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/oauth/token":
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer tok-1"
        if request.url.path == "/v1/clients/CL-1":
            return httpx.Response(200, json={"data": {"client_id": "CL-1", "person": {}}})
        if request.url.path == "/v1/clients":
            return httpx.Response(
                200, json={"items": [{"client_id": "CL-2"}, {"client_id": "CL-1"}]}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _rest(calls):
    auth = OAuth2ClientCredentials("https://idp.example.test/oauth/token", "id", "secret")
    return RestCrmSource(
        "https://crm.example.test/v1", auth, record_key="data", transport=_crm_transport(calls)
    )


def test_rest_source_fetches_with_cached_client_credentials_token():
    calls: list[httpx.Request] = []
    crm = _rest(calls)
    assert crm.get_client("CL-1")["client_id"] == "CL-1"
    assert crm.list_clients() == ["CL-1", "CL-2"]
    token_calls = [c for c in calls if c.url.path == "/oauth/token"]
    assert len(token_calls) == 1  # second call reused the token
    assert {c.method for c in calls if c.url.host == "crm.example.test"} == {"GET"}


def test_rest_source_maps_404_and_refuses_writes():
    calls: list[httpx.Request] = []
    crm = _rest(calls)
    with pytest.raises(ClientNotFound):
        crm.get_client("CL-404")
    before = len(calls)
    with pytest.raises(ReadOnlyViolation):
        crm._request("PATCH", "/clients/CL-1")
    assert len(calls) == before  # refused before any request was sent


def test_oauth_token_is_refreshed_after_expiry():
    issued = iter(["tok-a", "tok-b"])

    def handler(request):
        return httpx.Response(200, json={"access_token": next(issued), "expires_in": 100})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    auth = OAuth2ClientCredentials("https://idp.example.test/t", "id", "s", leeway_seconds=10)
    assert auth.token(http, now=0) == "tok-a"
    assert auth.token(http, now=50) == "tok-a"
    assert auth.token(http, now=95) == "tok-b"
