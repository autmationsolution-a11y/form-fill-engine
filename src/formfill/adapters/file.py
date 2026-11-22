"""File-based adapters: a JSON export or a CSV export from a CRM.

CSV columns use dotted paths (``person.first_name``, ``beneficiaries[0].share``) and are
rebuilt into nested records, so the same mapping file works for both formats.
"""

from __future__ import annotations

import csv
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..paths import parse_path
from .base import ClientDataSource, ClientNotFound, ClientRecord


class JsonFileSource(ClientDataSource):
    """A JSON file holding either a list of records or ``{"clients": [...]}``."""

    def __init__(self, path: Path | str, id_field: str = "client_id") -> None:
        self.path = Path(path)
        self.id_field = id_field
        data = json.loads(self.path.read_text(encoding="utf-8"))
        records = data.get("clients", []) if isinstance(data, dict) else data
        self._records = {str(r[id_field]): r for r in records}

    def _fetch(self, client_id: str) -> ClientRecord:
        try:
            return self._records[client_id]
        except KeyError:
            raise ClientNotFound(client_id) from None

    def list_clients(self) -> list[str]:
        return sorted(self._records)

    def describe(self) -> str:
        return f"json:{self.path.name}"


_NUMBER = re.compile(r"-?(0|[1-9]\d*)(\.\d+)?")


def _coerce(raw: str) -> Any:
    """CSV is text only. Recover booleans and plain numbers; leave everything else as text.

    Values such as ``01234`` (postcodes, IDs) or ``+356...`` (phone numbers) stay text, because
    turning them into numbers silently drops characters.
    """
    text = raw.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text == "":
        return None
    if _NUMBER.fullmatch(text):
        return float(text) if "." in text else int(text)
    return raw


def _set_path(record: dict[str, Any], path: str, value: Any) -> None:
    tokens = parse_path(path)
    current: Any = record
    for token, nxt in pairwise(tokens):
        container: Any = [] if isinstance(nxt, int) else {}
        if isinstance(token, int):
            while len(current) <= token:
                current.append(None)
            if current[token] is None:
                current[token] = container
            current = current[token]
        else:
            current = current.setdefault(token, container)
    last = tokens[-1]
    if isinstance(last, int):
        while len(current) <= last:
            current.append(None)
    current[last] = value


def unflatten(row: dict[str, str], coerce_keys: frozenset[str] = frozenset()) -> ClientRecord:
    record: ClientRecord = {}
    for key, raw in row.items():
        if key is None:
            continue
        value: Any = raw if key in coerce_keys else _coerce(raw)
        if value is None:
            continue
        _set_path(record, key, value)
    return record


class CsvFileSource(ClientDataSource):
    """A CSV export with dotted-path column headers. Text columns stay text."""

    def __init__(
        self,
        path: Path | str,
        id_field: str = "client_id",
        text_columns: tuple[str, ...] = (),
    ) -> None:
        self.path = Path(path)
        self.id_field = id_field
        keep = frozenset(text_columns) | {id_field}
        with self.path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self._records = {row[id_field]: unflatten(row, keep) for row in rows}

    def _fetch(self, client_id: str) -> ClientRecord:
        try:
            return self._records[client_id]
        except KeyError:
            raise ClientNotFound(client_id) from None

    def list_clients(self) -> list[str]:
        return sorted(self._records)

    def describe(self) -> str:
        return f"csv:{self.path.name}"
