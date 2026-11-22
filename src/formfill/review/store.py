"""Review packages: the unit a person approves or rejects.

Layout under the work directory::

    packages/<package_id>/filled.pdf      the filled form, exactly as the reviewer sees it
    packages/<package_id>/package.json    metadata, review report, hashes, decision
    outbox/<package_id>/<file>.pdf        released copy, only after approval
    outbox/<package_id>/manifest.json     what was released, by whom, with which hash
    audit.jsonl                           hash-chained audit log

Release rules, enforced here rather than in the web layer so the CLI and any future
integration get the same guarantees:

1. Only ``pending`` packages can be decided, and only once.
2. Packages with blocking issues (missing or invalid fields) cannot be approved.
3. Four-eyes: the person who prepared a package cannot approve it.
4. The PDF on disk must still match the hash recorded when it was filled.
5. Nothing reaches the outbox without an approval entry in the audit log.
"""

from __future__ import annotations

import json
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import FillReport, render
from ..pdf import flatten_pdf, sha256_file
from ..registry import TemplateMapping, load_mapping
from .audit import AuditLog

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"


class ReviewError(Exception):
    """A decision was refused. ``code`` maps to an HTTP status in the web app."""

    def __init__(self, message: str, code: int = 409) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Package:
    data: dict[str, Any]
    directory: Path

    @property
    def id(self) -> str:
        return self.data["package_id"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def pdf_path(self) -> Path:
        return self.directory / "filled.pdf"

    @property
    def report(self) -> dict[str, Any]:
        return self.data["report"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_actor(name: str) -> str:
    actor = (name or "").strip()
    if not actor:
        raise ReviewError("a named person is required for this action", code=400)
    return actor


class PackageStore:
    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir)
        self.packages_dir = self.workdir / "packages"
        self.outbox_dir = self.workdir / "outbox"
        self.audit = AuditLog(self.workdir / "audit.jsonl")

    # -- creation -----------------------------------------------------------------------

    def create(
        self,
        mapping: TemplateMapping,
        report: FillReport,
        prepared_by: str,
        source: str = "",
    ) -> Package:
        prepared_by = _clean_actor(prepared_by)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        package_id = f"PKG-{stamp}-{secrets.token_hex(3)}"
        directory = self.packages_dir / package_id
        directory.mkdir(parents=True)
        pdf_path = render(mapping, report, directory / "filled.pdf")
        data = {
            "package_id": package_id,
            "status": PENDING,
            "template_id": mapping.template_id,
            "template_title": mapping.title,
            "template_version": mapping.version,
            "provider": mapping.provider,
            "mapping_path": str(mapping.mapping_path),
            "mapping_sha256": sha256_file(mapping.mapping_path),
            "template_sha256": sha256_file(mapping.pdf_path),
            "flatten_on_release": mapping.flatten,
            "client_id": report.client_id,
            "source": source,
            "prepared_by": prepared_by,
            "created_at": _now(),
            "pdf_sha256": sha256_file(pdf_path),
            "report": report.to_dict(),
            "decision": None,
        }
        self._write(directory, data)
        self.audit.append(
            "package.created",
            prepared_by,
            package_id=package_id,
            template_id=mapping.template_id,
            template_version=mapping.version,
            client_id=report.client_id,
            pdf_sha256=data["pdf_sha256"],
            mapping_sha256=data["mapping_sha256"],
            summary=report.summary(),
            blocking=report.blocking,
        )
        return Package(data, directory)

    # -- queries ------------------------------------------------------------------------

    def get(self, package_id: str) -> Package:
        if "/" in package_id or "\\" in package_id or package_id.startswith("."):
            raise ReviewError("invalid package id", code=400)
        directory = self.packages_dir / package_id
        meta = directory / "package.json"
        if not meta.exists():
            raise ReviewError(f"package {package_id} not found", code=404)
        return Package(json.loads(meta.read_text(encoding="utf-8")), directory)

    def list(self, status: str | None = None) -> list[Package]:
        if not self.packages_dir.exists():
            return []
        found = []
        for meta in sorted(self.packages_dir.glob("*/package.json"), reverse=True):
            pkg = Package(json.loads(meta.read_text(encoding="utf-8")), meta.parent)
            if status is None or pkg.status == status:
                found.append(pkg)
        return found

    # -- decisions ----------------------------------------------------------------------

    def approve(self, package_id: str, reviewer: str, comment: str = "") -> Package:
        reviewer = _clean_actor(reviewer)
        pkg = self._pending(package_id)
        if pkg.report["blocking"]:
            raise ReviewError("package has missing or invalid fields; reject and correct the data")
        if reviewer.casefold() == pkg.data["prepared_by"].casefold():
            raise ReviewError("four-eyes rule: the preparer cannot approve their own package", 403)
        self._check_integrity(pkg)

        release_dir = self.outbox_dir / pkg.id
        release_name = f"{pkg.data['template_id']}_{pkg.data['client_id']}.pdf"
        release_path = release_dir / release_name
        if pkg.data["flatten_on_release"]:
            flatten_pdf(pkg.pdf_path, release_path)
        else:
            release_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pkg.pdf_path, release_path)
        released_sha = sha256_file(release_path)

        decided_at = _now()
        pkg.data["status"] = APPROVED
        pkg.data["decision"] = {
            "by": reviewer,
            "at": decided_at,
            "comment": comment.strip(),
            "released_file": str(release_path),
            "released_sha256": released_sha,
            "doubtful_acknowledged": pkg.report["summary"].get("doubtful", 0),
        }
        manifest = {
            "package_id": pkg.id,
            "template_id": pkg.data["template_id"],
            "template_version": pkg.data["template_version"],
            "client_id": pkg.data["client_id"],
            "prepared_by": pkg.data["prepared_by"],
            "approved_by": reviewer,
            "approved_at": decided_at,
            "reviewed_pdf_sha256": pkg.data["pdf_sha256"],
            "released_pdf_sha256": released_sha,
            "flattened": pkg.data["flatten_on_release"],
        }
        (release_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
        self._write(pkg.directory, pkg.data)
        self.audit.append(
            "package.approved",
            reviewer,
            package_id=pkg.id,
            template_id=pkg.data["template_id"],
            client_id=pkg.data["client_id"],
            reviewed_pdf_sha256=pkg.data["pdf_sha256"],
            released_pdf_sha256=released_sha,
            doubtful_acknowledged=pkg.data["decision"]["doubtful_acknowledged"],
            comment=comment.strip(),
        )
        return pkg

    def reject(self, package_id: str, reviewer: str, reason: str) -> Package:
        reviewer = _clean_actor(reviewer)
        if not (reason or "").strip():
            raise ReviewError("a rejection needs a reason", code=400)
        pkg = self._pending(package_id)
        pkg.data["status"] = REJECTED
        pkg.data["decision"] = {"by": reviewer, "at": _now(), "reason": reason.strip()}
        self._write(pkg.directory, pkg.data)
        self.audit.append(
            "package.rejected",
            reviewer,
            package_id=pkg.id,
            template_id=pkg.data["template_id"],
            client_id=pkg.data["client_id"],
            pdf_sha256=pkg.data["pdf_sha256"],
            reason=reason.strip(),
        )
        return pkg

    # -- internals ----------------------------------------------------------------------

    def _pending(self, package_id: str) -> Package:
        pkg = self.get(package_id)
        if pkg.status != PENDING:
            raise ReviewError(f"package {package_id} is already {pkg.status}")
        return pkg

    def _check_integrity(self, pkg: Package) -> None:
        if sha256_file(pkg.pdf_path) != pkg.data["pdf_sha256"]:
            self.audit.append("package.tamper_detected", "system", package_id=pkg.id)
            raise ReviewError("filled PDF changed since it was prepared; re-run the fill")
        mapping_path = Path(pkg.data["mapping_path"])
        if mapping_path.exists() and sha256_file(mapping_path) != pkg.data["mapping_sha256"]:
            raise ReviewError("mapping file changed since the package was prepared; re-run")
        if mapping_path.exists():
            mapping = load_mapping(mapping_path)
            if sha256_file(mapping.pdf_path) != pkg.data["template_sha256"]:
                raise ReviewError("blank PDF template changed since preparation; re-run")

    @staticmethod
    def _write(directory: Path, data: dict[str, Any]) -> None:
        tmp = directory / "package.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(directory / "package.json")
