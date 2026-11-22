"""Read-only client data source interface.

The engine reads client records; it never writes back to the system of record. Adapters
expose ``get_client`` and ``list_clients`` and nothing else. There is no ``update`` method to
misuse, and every record returned is a deep copy, so code downstream cannot mutate the
adapter's cache by accident.
"""

from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Any

ClientRecord = dict[str, Any]


class ClientNotFound(KeyError):
    pass


class ReadOnlyViolation(RuntimeError):
    """Raised if any code path attempts a write against a data source."""


class ClientDataSource(ABC):
    """Abstract read-only CRM or system-of-record interface."""

    read_only = True

    @abstractmethod
    def _fetch(self, client_id: str) -> ClientRecord:
        """Return the raw record for ``client_id`` or raise :class:`ClientNotFound`."""

    @abstractmethod
    def list_clients(self) -> list[str]:
        """Return the client identifiers this source can serve."""

    def get_client(self, client_id: str) -> ClientRecord:
        return copy.deepcopy(self._fetch(client_id))

    def describe(self) -> str:
        return type(self).__name__
