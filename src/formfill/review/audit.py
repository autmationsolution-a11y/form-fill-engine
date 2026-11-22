"""Append-only, hash-chained audit log in JSON Lines.

Every entry carries the SHA-256 of the previous entry. Editing, deleting or reordering any
line breaks the chain, and :meth:`AuditLog.verify` reports the first broken sequence number.
This is tamper-evident, not tamper-proof: for stronger guarantees, ship the log to WORM
storage or anchor the latest hash in an external system.

Entries hold identifiers and hashes, never client field values, so the log itself does not
become a second copy of personal data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: Path | str, clock: Callable[[], str] = _utc_now) -> None:
        self.path = Path(path)
        self.clock = clock

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def append(self, event: str, actor: str, **details: Any) -> dict[str, Any]:
        existing = self.entries()
        prev = existing[-1] if existing else None
        entry: dict[str, Any] = {
            "seq": (prev["seq"] + 1) if prev else 1,
            "ts": self.clock(),
            "event": event,
            "actor": actor,
            **details,
            "prev_hash": prev["entry_hash"] if prev else GENESIS,
        }
        entry["entry_hash"] = _digest(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        prev_hash = GENESIS
        try:
            entries = self.entries()
        except json.JSONDecodeError as exc:
            return False, f"unreadable line: {exc}"
        for expected_seq, entry in enumerate(entries, start=1):
            if entry.get("seq") != expected_seq:
                return False, f"sequence break at entry {expected_seq}"
            if entry.get("prev_hash") != prev_hash:
                return False, f"chain broken at seq {expected_seq}: prev_hash mismatch"
            if entry.get("entry_hash") != _digest(entry):
                return False, f"chain broken at seq {expected_seq}: entry altered"
            prev_hash = entry["entry_hash"]
        return True, f"{len(entries)} entries, chain intact"
