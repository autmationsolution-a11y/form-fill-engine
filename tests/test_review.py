"""Review gate: packages, four-eyes approval, release to outbox, audit chain, web app."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from formfill.engine import prepare
from formfill.pdf import sha256_file
from formfill.review.app import create_app
from formfill.review.audit import AuditLog
from formfill.review.store import APPROVED, PENDING, REJECTED, PackageStore, ReviewError


@pytest.fixture()
def store(tmp_path):
    return PackageStore(tmp_path / "work")


def _package(store, registry, source, today, template="northwind_beneficiary_change",
             client="CL-1001", preparer="pat.preparer"):  # fmt: skip
    mapping = registry.get(template)
    report = prepare(mapping, source.get_client(client), client, today=today)
    return store.create(mapping, report, prepared_by=preparer, source=source.describe())


# -- store -------------------------------------------------------------------------------


def test_create_package_writes_pdf_metadata_and_audit_entry(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    assert pkg.status == PENDING
    assert pkg.pdf_path.exists()
    assert pkg.data["pdf_sha256"] == sha256_file(pkg.pdf_path)
    (entry,) = store.audit.entries()
    assert entry["event"] == "package.created" and entry["actor"] == "pat.preparer"
    assert entry["pdf_sha256"] == pkg.data["pdf_sha256"]
    assert not (store.outbox_dir / pkg.id).exists()  # nothing released before approval


def test_approval_releases_to_outbox_with_manifest_and_hashes(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    approved = store.approve(pkg.id, "riley.reviewer", "checked against client file")
    assert approved.status == APPROVED
    release_dir = store.outbox_dir / pkg.id
    manifest = json.loads((release_dir / "manifest.json").read_text())
    released = release_dir / "northwind_beneficiary_change_CL-1001.pdf"
    assert manifest["approved_by"] == "riley.reviewer"
    assert manifest["released_pdf_sha256"] == sha256_file(released)
    assert manifest["reviewed_pdf_sha256"] == pkg.data["pdf_sha256"]
    events = [e["event"] for e in store.audit.entries()]
    assert events == ["package.created", "package.approved"]
    assert store.audit.verify()[0]


def test_four_eyes_rule_blocks_self_approval(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    with pytest.raises(ReviewError, match="four-eyes") as err:
        store.approve(pkg.id, "PAT.PREPARER")
    assert err.value.code == 403
    assert store.get(pkg.id).status == PENDING


def test_blocking_package_cannot_be_approved_only_rejected(store, registry, source, today):
    pkg = _package(
        store, registry, source, today, template="bluepeak_sepa_mandate", client="CL-1002"
    )
    with pytest.raises(ReviewError, match="missing or invalid"):
        store.approve(pkg.id, "riley.reviewer")
    with pytest.raises(ReviewError, match="reason"):
        store.reject(pkg.id, "riley.reviewer", "  ")
    rejected = store.reject(pkg.id, "riley.reviewer", "IBAN checksum fails; ask client")
    assert rejected.status == REJECTED
    assert not (store.outbox_dir / pkg.id).exists()


def test_a_decision_is_final(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    store.reject(pkg.id, "riley.reviewer", "wrong form version requested")
    with pytest.raises(ReviewError, match="already rejected"):
        store.approve(pkg.id, "riley.reviewer")


def test_tampered_pdf_is_detected_before_release(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    with pkg.pdf_path.open("ab") as handle:
        handle.write(b"\n% edited after preparation\n")
    with pytest.raises(ReviewError, match="changed since"):
        store.approve(pkg.id, "riley.reviewer")
    assert store.audit.entries()[-1]["event"] == "package.tamper_detected"
    assert not (store.outbox_dir / pkg.id).exists()


def test_changed_mapping_invalidates_pending_package(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    mapping_path = registry.get("northwind_beneficiary_change").mapping_path
    mapping_path.write_text(mapping_path.read_text() + "\n# edited\n")
    with pytest.raises(ReviewError, match="mapping file changed"):
        store.approve(pkg.id, "riley.reviewer")


def test_flatten_on_release_produces_a_non_editable_pdf(store, registry, source, today):
    pkg = _package(store, registry, source, today, template="harborline_tax_residence")
    approved = store.approve(pkg.id, "riley.reviewer")
    reader = PdfReader(approved.data["decision"]["released_file"])
    assert not reader.get_fields()
    assert "HPT-778812" in reader.pages[0].extract_text()


def test_package_ids_cannot_escape_the_work_directory(store):
    with pytest.raises(ReviewError) as err:
        store.get("../../etc")
    assert err.value.code == 400


# -- audit -------------------------------------------------------------------------------


def test_audit_chain_detects_edits_and_deletions(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl", clock=lambda: "2026-11-22T10:00:00+00:00")
    for i in range(3):
        log.append("event", "someone", n=i)
    assert log.verify() == (True, "3 entries, chain intact")

    lines = log.path.read_text().splitlines()
    edited = json.loads(lines[1])
    edited["actor"] = "someone else"
    log.path.write_text("\n".join([lines[0], json.dumps(edited), lines[2]]) + "\n")
    ok, message = log.verify()
    assert not ok and "seq 2" in message

    log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not log.verify()[0]


def test_audit_entries_hold_identifiers_not_client_values(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    store.approve(pkg.id, "riley.reviewer")
    raw = store.audit.path.read_text()
    for value in ("Alex", "Example", "XA1234567", "alex.example@example.org"):
        assert value not in raw


# -- web app -----------------------------------------------------------------------------


def test_review_page_shows_pdf_values_next_to_source_values(store, registry, source, today):
    pkg = _package(store, registry, source, today, client="CL-1003")
    client = TestClient(create_app(store.workdir))
    index = client.get("/")
    assert index.status_code == 200 and pkg.id in index.text
    page = client.get(f"/packages/{pkg.id}")
    assert page.status_code == 200
    assert "kyc.last_review" in page.text and "2024-03-10" in page.text
    assert "10/03/2024" in page.text  # the value as written into the PDF
    assert "doubtful" in page.text
    assert "differ from the engine output" not in page.text
    pdf = client.get(f"/packages/{pkg.id}/pdf")
    assert pdf.headers["content-type"] == "application/pdf"


def test_web_approval_and_api(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    client = TestClient(create_app(store.workdir))
    refused = client.post(f"/packages/{pkg.id}/approve", data={"reviewer": "pat.preparer"})
    assert refused.status_code == 403
    done = client.post(
        f"/packages/{pkg.id}/approve", data={"reviewer": "riley.reviewer"}, follow_redirects=False
    )
    assert done.status_code == 303
    (listed,) = client.get("/api/packages").json()
    assert listed["status"] == "approved"
    assert client.get("/api/audit/verify").json()["ok"] is True


def test_trusted_header_mode_ignores_typed_names(store, registry, source, today):
    pkg = _package(store, registry, source, today)
    client = TestClient(create_app(store.workdir, trusted_user_header="X-Forwarded-User"))
    no_identity = client.post(
        f"/packages/{pkg.id}/reject", data={"reviewer": "anyone", "reason": "x"}
    )
    assert no_identity.status_code == 401
    client.post(
        f"/packages/{pkg.id}/reject",
        data={"reviewer": "typed.name", "reason": "duplicate request"},
        headers={"X-Forwarded-User": "sso.user@example.org"},
    )
    assert store.get(pkg.id).data["decision"]["by"] == "sso.user@example.org"
