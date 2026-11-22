"""The fill engine: mapping + client record -> field values + review report -> filled PDF.

The engine is deliberately split in two steps. :func:`prepare` is pure: it resolves, transforms
and validates, and touches no files. :func:`render` writes the PDF. ``formfill validate`` runs
only the first step; ``formfill fill`` runs both and hands the result to the review queue.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from . import conditions, transforms
from .paths import MISSING, resolve
from .pdf import PdfField, fill_pdf, inspect_fields
from .registry import FieldMapping, TemplateMapping
from .validators import Issue, ValidationContext, run_rule


class Status(str, Enum):
    FILLED = "filled"  # value written, all checks passed
    DOUBTFUL = "doubtful"  # value written, a warning needs a human look
    MISSING = "missing"  # required value absent: blocks approval
    INVALID = "invalid"  # value failed a hard check: not written, blocks approval
    EMPTY = "empty"  # optional and absent: left blank
    SKIPPED = "skipped"  # condition not met: left blank on purpose


BLOCKING = {Status.MISSING, Status.INVALID}
WRITTEN = {Status.FILLED, Status.DOUBTFUL}


@dataclass
class FieldResult:
    pdf_field: str
    label: str
    type: str
    source: str
    source_value: str
    output_value: Any
    status: Status
    issues: list[Issue] = field(default_factory=list)


@dataclass
class FillReport:
    template_id: str
    template_version: str
    client_id: str
    fields: list[FieldResult]
    rule_issues: list[Issue]

    @property
    def blocking(self) -> bool:
        return any(f.status in BLOCKING for f in self.fields) or any(
            i.severity == "error" for i in self.rule_issues
        )

    def count(self, status: Status) -> int:
        return sum(1 for f in self.fields if f.status == status)

    def summary(self) -> dict[str, int]:
        return {s.value: self.count(s) for s in Status}

    def pdf_values(self) -> dict[str, Any]:
        return {f.pdf_field: f.output_value for f in self.fields if f.status in WRITTEN}

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "client_id": self.client_id,
            "blocking": self.blocking,
            "summary": self.summary(),
            "fields": [
                {
                    **{k: v for k, v in asdict(f).items() if k not in {"status", "issues"}},
                    "status": f.status.value,
                    "issues": [asdict(i) for i in f.issues],
                }
                for f in self.fields
            ],
            "rule_issues": [asdict(i) for i in self.rule_issues],
        }


def _display(value: Any) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " | ".join(_display(v) for v in value)
    return str(value)


def _is_empty(value: Any) -> bool:
    if value is MISSING or value is None or value == "":
        return True
    if isinstance(value, (list, tuple)):
        return all(_is_empty(v) for v in value)
    return False


def _raw_value(fm: FieldMapping, record: Any) -> Any:
    if fm.value is not None:
        return fm.value
    if fm.sources:
        return [resolve(record, s) for s in fm.sources]
    return resolve(record, fm.source or "")


def _field_result(
    fm: FieldMapping, record: Any, ctx: ValidationContext, pdf_field: PdfField | None
) -> FieldResult:
    def result(status: Status, raw: Any, output: Any = None, issues: list[Issue] | None = None):
        return FieldResult(
            pdf_field=fm.pdf_field,
            label=fm.label or fm.pdf_field,
            type=fm.type,
            source=fm.source_label,
            source_value=_display(raw),
            output_value=output,
            status=status,
            issues=issues or [],
        )

    if fm.when is not None and not conditions.evaluate(fm.when, record):
        return result(Status.SKIPPED, MISSING)

    raw = _raw_value(fm, record)
    if _is_empty(raw) and fm.default is not None:
        raw = fm.default
    if _is_empty(raw):
        if fm.required:
            return result(Status.MISSING, raw, issues=[Issue("required", "required value absent")])
        return result(Status.EMPTY, raw)

    issues: list[Issue] = []
    for spec in fm.validate:
        if spec.target == "source":
            issue = spec.run(raw, None, ctx)
            if issue:
                issues.append(issue)

    try:
        output = transforms.apply(raw, list(fm.transform))
    except transforms.TransformError as exc:
        issues.append(Issue("transform", str(exc)))
        return result(Status.INVALID, raw, issues=issues)

    if fm.type == "checkbox":
        output = bool(output)
    elif fm.type == "radio":
        output = str(output)
        allowed = fm.options or (pdf_field.states if pdf_field else ())
        if allowed and output not in allowed:
            issues.append(Issue("radio.option", f"{output!r} not in options {list(allowed)}"))
    else:
        output = str(output)
        if pdf_field and pdf_field.max_len and len(output) > pdf_field.max_len:
            issues.append(
                Issue(
                    "pdf.max_len",
                    f"{len(output)} chars, PDF field holds {pdf_field.max_len}",
                    "warning",
                )
            )

    for spec in fm.validate:
        if spec.target == "output":
            issue = spec.run(raw, output, ctx)
            if issue:
                issues.append(issue)

    if any(i.severity == "error" for i in issues):
        return result(Status.INVALID, raw, issues=issues)
    if issues:
        return result(Status.DOUBTFUL, raw, output, issues)
    return result(Status.FILLED, raw, output)


def prepare(
    mapping: TemplateMapping,
    record: dict[str, Any],
    client_id: str,
    today: date | None = None,
    pdf_fields: dict[str, PdfField] | None = None,
) -> FillReport:
    """Resolve, transform and validate every mapped field. Pure: no files are written."""
    ctx = ValidationContext(today=today or date.today())
    if pdf_fields is None and mapping.pdf_path.exists():
        pdf_fields = inspect_fields(mapping.pdf_path)
    pdf_fields = pdf_fields or {}

    results = [
        _field_result(fm, record, ctx, pdf_fields.get(fm.pdf_field)) for fm in mapping.fields
    ]
    rule_issues = [i for i in (run_rule(r, record) for r in mapping.rules) if i]

    # Attach cross-field issues to the fields that read those paths, so the reviewer sees them
    # on the right row, and downgrade those fields accordingly.
    by_source = {}
    for fm, fr in zip(mapping.fields, results, strict=True):
        for path in (fm.source, *fm.sources):
            if path:
                by_source.setdefault(path, []).append(fr)
    for issue in rule_issues:
        for path in issue.fields:
            for fr in by_source.get(path, []):
                fr.issues.append(issue)
                if issue.severity == "error" and fr.status in WRITTEN:
                    fr.status = Status.INVALID
                    fr.output_value = None
                elif issue.severity == "error" and fr.status == Status.EMPTY:
                    fr.status = Status.MISSING
                elif fr.status == Status.FILLED:
                    fr.status = Status.DOUBTFUL

    return FillReport(mapping.template_id, mapping.version, client_id, results, rule_issues)


def check_mapping(mapping: TemplateMapping) -> list[str]:
    """Problems between a mapping file and its PDF. Empty list means they agree."""
    if not mapping.pdf_path.exists():
        return [f"PDF not found: {mapping.pdf_path}"]
    pdf_fields = inspect_fields(mapping.pdf_path)
    problems = []
    for fm in mapping.fields:
        pf = pdf_fields.get(fm.pdf_field)
        if pf is None:
            problems.append(f"{fm.pdf_field}: not a field in the PDF")
            continue
        expected = {"text": {"text", "choice"}, "checkbox": {"checkbox"}, "radio": {"radio"}}
        if pf.kind not in expected[fm.type]:
            problems.append(f"{fm.pdf_field}: mapping says {fm.type}, PDF says {pf.kind}")
        if fm.type == "radio" and fm.options:
            extra = sorted(set(fm.options) - set(pf.states))
            if extra:
                problems.append(f"{fm.pdf_field}: options {extra} not in PDF {list(pf.states)}")
    return problems


def unmapped_fields(mapping: TemplateMapping) -> list[str]:
    if not mapping.pdf_path.exists():
        return []
    mapped = {fm.pdf_field for fm in mapping.fields}
    return sorted(set(inspect_fields(mapping.pdf_path)) - mapped)


def render(mapping: TemplateMapping, report: FillReport, out_path: Path | str) -> Path:
    """Write the filled PDF. Flattening happens at release, after approval, not here."""
    problems = check_mapping(mapping)
    if problems:
        raise ValueError("mapping does not match PDF: " + "; ".join(problems))
    return fill_pdf(mapping.pdf_path, report.pdf_values(), out_path, flatten=False)
