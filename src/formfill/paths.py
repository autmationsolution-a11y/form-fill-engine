"""Resolve dotted source paths such as ``beneficiaries[0].name`` in a nested client record."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


class _Missing:
    """Sentinel for a path that does not resolve. Distinct from ``None`` (an explicit null)."""

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Any = _Missing()


def parse_path(path: str) -> list[str | int]:
    """Split ``a.b[2].c`` into ``["a", "b", 2, "c"]``. Raises ``ValueError`` on bad syntax."""
    if not path or not path.strip():
        raise ValueError("empty source path")
    tokens: list[str | int] = []
    pos = 0
    path = path.strip()
    while pos < len(path):
        if path[pos] == ".":
            pos += 1
            continue
        match = _TOKEN.match(path, pos)
        if not match:
            raise ValueError(f"invalid source path: {path!r}")
        key, index = match.groups()
        tokens.append(int(index) if index is not None else key)
        pos = match.end()
    return tokens


def resolve(record: Any, path: str) -> Any:
    """Return the value at ``path`` or ``MISSING`` if any segment does not exist."""
    current = record
    for token in parse_path(path):
        if isinstance(token, int):
            if not isinstance(current, (list, tuple)) or token >= len(current):
                return MISSING
            current = current[token]
        else:
            if not isinstance(current, Mapping) or token not in current:
                return MISSING
            current = current[token]
    return current


def iter_leaf_paths(record: Any, prefix: str = "") -> Iterator[str]:
    """Yield every leaf path in a record. Used to build a schema without exposing values."""
    if isinstance(record, Mapping):
        for key, value in record.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from iter_leaf_paths(value, child)
    elif isinstance(record, (list, tuple)):
        for i, value in enumerate(record):
            yield from iter_leaf_paths(value, f"{prefix}[{i}]")
    else:
        yield prefix
