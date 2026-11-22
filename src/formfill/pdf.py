"""Thin, tested wrapper over pypdf for AcroForm work: inspect, fill, read back, flatten."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject


class PdfFormError(ValueError):
    pass


@dataclass(frozen=True)
class PdfField:
    name: str
    kind: str  # text | checkbox | radio | choice | signature | unknown
    states: tuple[str, ...] = ()  # export values for checkbox/radio, without the leading "/"
    max_len: int | None = None

    @property
    def on_state(self) -> str | None:
        on = [s for s in self.states if s != "Off"]
        return on[0] if self.kind == "checkbox" and on else None


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kind(field: dict[str, Any]) -> str:
    ft = field.get("/FT")
    flags = int(field.get("/Ff", 0))
    if ft == "/Tx":
        return "text"
    if ft == "/Btn":
        if flags & (1 << 15):  # radio flag
            return "radio"
        if flags & (1 << 16):  # push button: not fillable
            return "unknown"
        return "checkbox"
    if ft == "/Ch":
        return "choice"
    if ft == "/Sig":
        return "signature"
    return "unknown"


def inspect_fields(pdf_path: Path | str) -> dict[str, PdfField]:
    reader = PdfReader(str(pdf_path))
    raw = reader.get_fields() or {}
    max_lens: dict[str, int] = {}
    for page in reader.pages:
        for annot in page.get("/Annots") or []:
            obj = annot.get_object()
            if "/MaxLen" in obj and "/T" in obj:
                max_lens[str(obj["/T"])] = int(obj["/MaxLen"])
    fields: dict[str, PdfField] = {}
    for name, field in raw.items():
        kind = _kind(field)
        states = tuple(str(s).lstrip("/") for s in field.get("/_States_", []) or [])
        if kind == "radio":
            states = tuple(s for s in states if s != "Off")
        max_len = field.get("/MaxLen")
        name = str(name)
        fields[name] = PdfField(
            name=name,
            kind=kind,
            states=states,
            max_len=int(max_len) if max_len is not None else max_lens.get(name),
        )
    return fields


def _pdf_value(field: PdfField, value: Any) -> str:
    if field.kind == "checkbox":
        if not field.on_state:
            raise PdfFormError(f"checkbox {field.name!r} has no 'on' state")
        return f"/{field.on_state}" if bool(value) else "/Off"
    if field.kind == "radio":
        option = str(value)
        if option not in field.states:
            raise PdfFormError(f"radio {field.name!r} has no option {option!r}; has {field.states}")
        return f"/{option}"
    if field.kind in {"text", "choice"}:
        return "" if value is None else str(value)
    raise PdfFormError(f"field {field.name!r} of kind {field.kind!r} cannot be filled")


def fill_pdf(
    template_pdf: Path | str,
    values: dict[str, Any],
    out_path: Path | str,
    flatten: bool = False,
) -> Path:
    """Write ``values`` into a copy of ``template_pdf``. The template file is never modified.

    ``values`` maps PDF field names to text, booleans (checkboxes) or option names (radios).
    Unknown field names raise, so a renamed field in a new form version fails loudly.
    """
    fields = inspect_fields(template_pdf)
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise PdfFormError(f"fields not in {Path(template_pdf).name}: {unknown}")
    pdf_values = {name: _pdf_value(fields[name], v) for name, v in values.items()}

    writer = PdfWriter(clone_from=PdfReader(str(template_pdf)))
    # page=None applies the values to every page that carries the field.
    writer.update_page_form_field_values(None, pdf_values, auto_regenerate=False, flatten=flatten)
    if flatten:
        writer.remove_annotations(subtypes="/Widget")
        if "/AcroForm" in writer._root_object:
            del writer._root_object[NameObject("/AcroForm")]
    else:
        writer.set_need_appearances_writer(True)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
    return out


def flatten_pdf(src_path: Path | str, out_path: Path | str) -> Path:
    """Burn the current field values of ``src_path`` into page content and drop the form.

    Used at release time on the exact file the reviewer approved, so the flattened copy
    carries the reviewed values and nothing else.
    """
    reader = PdfReader(str(src_path))
    values = {
        name: str(field["/V"])
        for name, field in (reader.get_fields() or {}).items()
        if field.get("/V") not in (None, "")
    }
    writer = PdfWriter(clone_from=reader)
    writer.update_page_form_field_values(None, values, auto_regenerate=False, flatten=True)
    writer.remove_annotations(subtypes="/Widget")
    if "/AcroForm" in writer._root_object:
        del writer._root_object[NameObject("/AcroForm")]
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as handle:
        writer.write(handle)
    return out


def read_values(pdf_path: Path | str) -> dict[str, str]:
    """Read field values back out of a PDF, as the reviewer will see them.

    The review screen uses this, not the engine's in-memory values, so what is approved is
    what is actually in the file.
    """
    reader = PdfReader(str(pdf_path))
    out: dict[str, str] = {}
    for name, field in (reader.get_fields() or {}).items():
        value = field.get("/V")
        out[name] = "" if value is None else str(value).lstrip("/")
    return out
