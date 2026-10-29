"""Tiny JSON ledger so a crash between 'Zeus order submitted' and 'PDF uploaded to TPro'
never creates a second Zeus order for the same load on the next run."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

STAGES = ("skipped", "failed", "zeus_submitted", "pdf_downloaded", "uploaded")


class Ledger:
    def __init__(self, path: Path, on_save=None):
        self.path = path
        self.on_save = on_save                     # e.g. push the file to S3 after every change
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = self.path.with_suffix(".corrupt.json")
                self.path.replace(backup)
                self._data = {}

    def get(self, load_id: int) -> dict | None:
        return self._data.get(str(load_id))

    def stage(self, load_id: int) -> str | None:
        entry = self.get(load_id)
        return entry.get("stage") if entry else None

    def set(self, load_id: int, **fields) -> dict:
        entry = self._data.setdefault(str(load_id), {})
        if fields.get("stage") in ("zeus_submitted", "pdf_downloaded", "uploaded"):
            for stale in ("error", "step", "reasons"):   # leftovers from earlier failed/skipped attempts
                entry.pop(stale, None)
        entry.update(fields)
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save()
        return entry

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)
        if self.on_save:
            try:
                self.on_save(self.path)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning("ledger remote sync failed: %s", exc)
