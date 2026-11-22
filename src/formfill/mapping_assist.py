"""Optional mapping assistant: propose a draft YAML mapping for a new PDF form.

What it sends to a model: the PDF's field names and types, and the *paths* of a sample client
record (``person.first_name``), never the values. No client data leaves the machine.

What it produces: a draft file with ``status: draft``, no approver, and a confidence score
per field. The registry refuses drafts, so a suggestion cannot fill a form until a person
reviews it and runs ``formfill approve-mapping``.

Three suggesters share one interface:

* :class:`HeuristicSuggester`: deterministic token matching. Offline, used in tests and as a
  fallback.
* :class:`AnthropicSuggester` and :class:`OpenAISuggester`: thin HTTP clients. Both accept an
  injected ``httpx`` transport, so they are testable without network access.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx
import yaml

from .paths import iter_leaf_paths
from .pdf import PdfField, inspect_fields
from .registry import MappingError, load_mapping


@dataclass(frozen=True)
class Suggestion:
    pdf_field: str
    source: str | None
    confidence: float
    type: str = "text"
    rationale: str = ""


class MappingSuggester(ABC):
    name = "base"

    @abstractmethod
    def suggest(self, fields: list[PdfField], schema_paths: list[str]) -> list[Suggestion]: ...


_SPLIT = re.compile(r"[^a-z0-9]+")
_SYNONYMS = {
    "dob": "birth", "birthdate": "birth", "surname": "last", "lastname": "last",
    "firstname": "first", "forename": "first", "given": "first", "family": "last",
    "zip": "postcode", "postal": "postcode", "mail": "email", "tel": "phone",
    "acct": "account", "no": "number", "num": "number", "nr": "number",
}  # fmt: skip


def _tokens(text: str) -> set[str]:
    raw = _SPLIT.split(re.sub(r"\[\d+\]", " ", text.lower()))
    return {_SYNONYMS.get(t, t) for t in raw if t}


def _type_for(field: PdfField) -> str:
    return field.kind if field.kind in {"checkbox", "radio"} else "text"


class HeuristicSuggester(MappingSuggester):
    """Score each schema path against each field name by token overlap and string similarity."""

    name = "heuristic"

    def __init__(self, threshold: float = 0.35) -> None:
        self.threshold = threshold

    def suggest(self, fields: list[PdfField], schema_paths: list[str]) -> list[Suggestion]:
        out = []
        for field in fields:
            field_tokens = _tokens(field.name)
            best_path, best_score = None, 0.0
            for path in schema_paths:
                path_tokens = _tokens(path)
                leaf_tokens = _tokens(path.rsplit(".", 1)[-1])
                union = field_tokens | path_tokens
                overlap = len(field_tokens & path_tokens) / len(union) if union else 0.0
                leaf_hit = len(field_tokens & leaf_tokens) / max(len(field_tokens), 1)
                ratio = SequenceMatcher(None, field.name.lower(), path.lower()).ratio()
                score = round(0.45 * leaf_hit + 0.35 * overlap + 0.20 * ratio, 3)
                if score > best_score:
                    best_path, best_score = path, score
            if best_score < self.threshold:
                best_path = None
            out.append(
                Suggestion(
                    pdf_field=field.name,
                    source=best_path,
                    confidence=best_score if best_path else 0.0,
                    type=_type_for(field),
                    rationale="token match" if best_path else "no confident match",
                )
            )
        return out


PROMPT = """You map PDF form fields to paths in a client record schema.
Return only JSON: a list of objects with keys pdf_field, source, confidence (0 to 1), rationale.
Use source null when no path fits. Use only paths from the schema list. Never invent paths.

PDF fields (name, type):
{fields}

Schema paths:
{paths}
"""


def build_prompt(fields: list[PdfField], schema_paths: list[str]) -> str:
    field_lines = "\n".join(f"- {f.name} ({f.kind})" for f in fields)
    path_lines = "\n".join(f"- {p}" for p in schema_paths)
    return PROMPT.format(fields=field_lines, paths=path_lines)


def parse_model_output(
    text: str, fields: list[PdfField], schema_paths: list[str]
) -> list[Suggestion]:
    """Parse and sanitise model output. Unknown paths are dropped, never trusted."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("model output contains no JSON list")
    items = json.loads(match.group(0))
    by_name = {f.name: f for f in fields}
    known = set(schema_paths)
    seen: dict[str, Suggestion] = {}
    for item in items:
        name = item.get("pdf_field")
        if name not in by_name or name in seen:
            continue
        source = item.get("source")
        rationale = str(item.get("rationale", ""))[:200]
        if source is not None and source not in known:
            source, rationale = None, f"model proposed unknown path {item.get('source')!r}"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        seen[name] = Suggestion(
            name, source, confidence if source else 0.0, _type_for(by_name[name]), rationale
        )
    return [
        seen.get(f.name, Suggestion(f.name, None, 0.0, _type_for(f), "not returned by model"))
        for f in fields
    ]


class _HttpSuggester(MappingSuggester):
    url = ""
    env_key = ""
    env_model = "FORMFILL_LLM_MODEL"
    default_model = ""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.api_key = api_key or os.environ.get(self.env_key, "")
        if not self.api_key:
            raise RuntimeError(f"{self.name}: set {self.env_key} to use this suggester")
        self.model = model or os.environ.get(self.env_model) or self.default_model
        self._http = httpx.Client(transport=transport, timeout=timeout)

    @abstractmethod
    def _call(self, prompt: str) -> str: ...

    def suggest(self, fields: list[PdfField], schema_paths: list[str]) -> list[Suggestion]:
        return parse_model_output(
            self._call(build_prompt(fields, schema_paths)), fields, schema_paths
        )


class AnthropicSuggester(_HttpSuggester):
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"
    env_key = "ANTHROPIC_API_KEY"
    default_model = "claude-sonnet-4-5"

    def _call(self, prompt: str) -> str:
        response = self._http.post(
            self.url,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": self.model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        blocks = response.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class OpenAISuggester(_HttpSuggester):
    name = "openai"
    url = "https://api.openai.com/v1/chat/completions"
    env_key = "OPENAI_API_KEY"
    default_model = "gpt-4o-mini"

    def _call(self, prompt: str) -> str:
        response = self._http.post(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": [{"role": "user", "content": prompt}]},
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


SUGGESTERS: dict[str, type[MappingSuggester]] = {
    "heuristic": HeuristicSuggester,
    "fake": HeuristicSuggester,
    "anthropic": AnthropicSuggester,
    "openai": OpenAISuggester,
}


def schema_from_record(record: dict[str, Any]) -> list[str]:
    return sorted(set(iter_leaf_paths(record)))


def draft_mapping(
    pdf_path: Path,
    record: dict[str, Any],
    suggester: MappingSuggester,
    template_id: str | None = None,
    pdf_ref: str | None = None,
) -> dict[str, Any]:
    fields = list(inspect_fields(pdf_path).values())
    suggestions = suggester.suggest(fields, schema_from_record(record))
    entries = []
    for s in suggestions:
        entry: dict[str, Any] = {"pdf_field": s.pdf_field}
        if s.type != "text":
            entry["type"] = s.type
        if s.source:
            entry["source"] = s.source
        else:
            entry["value"] = None
            entry["todo"] = "no confident match: choose a source or delete this entry"
        entry["confidence"] = s.confidence
        if s.rationale:
            entry["rationale"] = s.rationale
        entries.append(entry)
    return {
        "template_id": template_id or pdf_path.stem,
        "title": pdf_path.stem.replace("_", " ").title(),
        "provider": "",
        "version": "1",
        "pdf": pdf_ref or str(pdf_path),
        "status": "draft",
        "approved_by": None,
        "suggested_by": suggester.name,
        "fields": entries,
    }


def write_draft(draft: dict[str, Any], out_path: Path) -> Path:
    header = (
        "# DRAFT mapping suggested by software. Not usable until a person reviews every\n"
        "# entry, removes the confidence/rationale/todo keys, and runs:\n"
        "#   formfill approve-mapping <this file> --by <name>\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(draft, sort_keys=False, allow_unicode=True)
    out_path.write_text(header + body, encoding="utf-8")
    return out_path


_SUGGESTION_KEYS = {"confidence", "rationale", "todo"}


def approve_draft(draft_path: Path, approver: str, target_dir: Path) -> Path:
    """Promote a reviewed draft into the registry directory.

    Refuses if any suggestion-only key is still present, so approval means someone actually
    went through the file, not that a flag was flipped.
    """
    approver = approver.strip()
    if not approver:
        raise ValueError("an approver name is required")
    data = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    leftover = sorted({k for f in data.get("fields", []) for k in f if k in _SUGGESTION_KEYS})
    if leftover:
        raise ValueError(f"draft still contains review markers {leftover}; edit it first")
    unresolved = [f["pdf_field"] for f in data["fields"] if f.get("value", 0) is None]
    if unresolved:
        raise ValueError(f"fields without a source: {unresolved}; map or delete them")
    data["status"] = "approved"
    data["approved_by"] = approver
    pdf_abs = (draft_path.parent / data["pdf"]).resolve()
    data["pdf"] = Path(os.path.relpath(pdf_abs, target_dir.resolve())).as_posix()
    target = target_dir / f"{data['template_id']}.yaml"
    if target.exists():
        raise FileExistsError(f"{target} exists; bump the version in a new file instead")
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")
    try:
        load_mapping(target)
    except MappingError:
        target.unlink()
        raise
    return target
