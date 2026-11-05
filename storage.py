"""Per-server settings, kept in memory and saved to a JSON file on every change."""

from __future__ import annotations

import json
import os
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            # Starting empty would overwrite every server's settings on the next save.
            raise SystemExit(f"{self.path} is corrupted ({exc}). Fix or delete it, then restart.") from exc

    def get(self, guild_id: int) -> dict:
        return dict(self._data.get(str(guild_id), {}))

    def update(self, guild_id: int, **changes) -> None:
        """Set the given values. A value of None removes that setting."""
        entry = self._data.setdefault(str(guild_id), {})
        for key, value in changes.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        self._save()

    def _save(self) -> None:
        # Write then swap, so a crash mid-write can't leave a truncated file.
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)
