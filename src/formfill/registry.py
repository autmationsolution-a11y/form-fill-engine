"""Template registry: one YAML mapping file per PDF form template.

Only mappings with ``status: approved`` and a named ``approved_by`` are loadable for filling.
Drafts (for example LLM suggestions) are listed but never used. That is the gate that keeps a
machine-suggested mapping from reaching a client form without a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import conditions, transforms, validators
from .paths import parse_path

FIELD_TYPES = {"text", "checkbox", "radio"}


class MappingError(ValueError):
    """A mapping file is malformed. The message names the file and the field."""


@dataclass(frozen=True)
class FieldMapping:
    pdf_field: str
    type: str = "text"
    label: str = ""
    source: str | None = None
    sources: tuple[str, ...] = ()
    value: Any = None
    default: Any = None
    required: bool = False
    when: dict[str, Any] | None = None
    transform: tuple[tuple[str, dict[str, Any]], ...] = ()
    validate: tuple[validators.ValidatorSpec, ...] = ()
    options: tuple[str, ...] = ()

    @property
    def source_label(self) -> str:
        if self.source:
            return self.source
        if self.sources:
            return " + ".join(self.sources)
        return "(constant)" if self.value is not None else "(none)"


@dataclass(frozen=True)
class TemplateMapping:
    template_id: str
    title: str
    provider: str
    version: str
    status: str
    approved_by: str | None
    pdf_path: Path
    mapping_path: Path
    fields: tuple[FieldMapping, ...]
    rules: tuple[dict[str, Any], ...] = ()
    flatten: bool = False

    @property
    def is_approved(self) -> bool:
        return self.status == "approved" and bool(self.approved_by)


def _parse_field(raw: Any, where: str) -> FieldMapping:
    if not isinstance(raw, dict) or "pdf_field" not in raw:
        raise MappingError(f"{where}: each field needs 'pdf_field'")
    name = raw["pdf_field"]
    loc = f"{where} field {name!r}"
    kind = raw.get("type", "text")
    if kind not in FIELD_TYPES:
        raise MappingError(f"{loc}: type must be one of {sorted(FIELD_TYPES)}")
    sources_given = sum(1 for key in ("source", "sources", "value") if key in raw)
    if sources_given != 1:
        raise MappingError(f"{loc}: give exactly one of 'source', 'sources' or 'value'")
    try:
        for path in [raw.get("source"), *(raw.get("sources") or [])]:
            if path is not None:
                parse_path(path)
        if raw.get("when") is not None:
            conditions.check_spec(raw["when"])
        transform_spec = tuple(transforms.parse_spec(raw.get("transform")))
        validate_spec = tuple(validators.parse_spec(raw.get("validate")))
    except ValueError as exc:
        raise MappingError(f"{loc}: {exc}") from exc
    return FieldMapping(
        pdf_field=name,
        type=kind,
        label=raw.get("label", ""),
        source=raw.get("source"),
        sources=tuple(raw.get("sources") or ()),
        value=raw.get("value"),
        default=raw.get("default"),
        required=bool(raw.get("required", False)),
        when=raw.get("when"),
        transform=transform_spec,
        validate=validate_spec,
        options=tuple(str(o) for o in raw.get("options") or ()),
    )


def load_mapping(path: Path) -> TemplateMapping:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError(f"{path.name}: top level must be a mapping")
    for key in ("template_id", "pdf", "fields"):
        if key not in data:
            raise MappingError(f"{path.name}: missing required key '{key}'")
    fields = tuple(_parse_field(f, path.name) for f in data["fields"])
    names = [f.pdf_field for f in fields]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise MappingError(f"{path.name}: duplicate pdf_field entries {duplicates}")
    rules = tuple(data.get("rules") or ())
    for rule in rules:
        try:
            validators.check_rule_spec(rule)
        except ValueError as exc:
            raise MappingError(f"{path.name} rule {rule.get('name', '?')!r}: {exc}") from exc
    return TemplateMapping(
        template_id=str(data["template_id"]),
        title=str(data.get("title", data["template_id"])),
        provider=str(data.get("provider", "")),
        version=str(data.get("version", "1")),
        status=str(data.get("status", "draft")),
        approved_by=data.get("approved_by"),
        pdf_path=(path.parent / data["pdf"]).resolve(),
        mapping_path=path.resolve(),
        fields=fields,
        rules=rules,
        flatten=bool(data.get("flatten", False)),
    )


@dataclass
class TemplateRegistry:
    directory: Path
    templates: dict[str, TemplateMapping] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_directory(cls, directory: Path | str) -> TemplateRegistry:
        registry = cls(Path(directory))
        files = sorted(registry.directory.glob("*.yaml")) + sorted(registry.directory.glob("*.yml"))
        for path in files:
            try:
                mapping = load_mapping(path)
            except MappingError as exc:
                registry.errors[path.name] = str(exc)
                continue
            if mapping.template_id in registry.templates:
                registry.errors[path.name] = f"duplicate template_id {mapping.template_id!r}"
                continue
            registry.templates[mapping.template_id] = mapping
        return registry

    def list(self) -> list[TemplateMapping]:
        return [self.templates[k] for k in sorted(self.templates)]

    def get(self, template_id: str) -> TemplateMapping:
        """Return an approved template. Drafts raise: they must not be used to fill forms."""
        if template_id not in self.templates:
            known = ", ".join(sorted(self.templates)) or "none"
            raise KeyError(f"unknown template {template_id!r}; known: {known}")
        mapping = self.templates[template_id]
        if not mapping.is_approved:
            raise PermissionError(
                f"template {template_id!r} is '{mapping.status}' and has no approver; "
                "a person must review and approve the mapping before it can fill forms"
            )
        return mapping
