"""Read-only client data source adapters."""

from __future__ import annotations

from pathlib import Path

from .base import ClientDataSource, ClientNotFound, ReadOnlyViolation
from .file import CsvFileSource, JsonFileSource
from .rest import OAuth2ClientCredentials, RestCrmSource

__all__ = [
    "ClientDataSource",
    "ClientNotFound",
    "CsvFileSource",
    "JsonFileSource",
    "OAuth2ClientCredentials",
    "ReadOnlyViolation",
    "RestCrmSource",
    "open_source",
]


def open_source(path: Path | str) -> ClientDataSource:
    """Pick a file adapter from the extension. REST sources are built in code, not by path."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return JsonFileSource(path)
    if suffix == ".csv":
        return CsvFileSource(path)
    raise ValueError(f"unsupported data source {path.name!r}: use .json or .csv")
