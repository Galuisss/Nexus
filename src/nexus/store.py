"""Small, human-readable store for machine configuration and push subscriptions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {
                "machines": [
                    {
                        "id": "local",
                        "name": "本机",
                        "transport": "local",
                        "codexBin": "codex",
                        "connection": "daemon",
                        "projects": [],
                        "port": 22,
                    }
                ]
            }
            self.save()
        if any("cwd" in machine for machine in self.machines):
            for machine in self.machines:
                machine.pop("cwd", None)
            self.save()

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.path)

    @property
    def machines(self) -> list[dict[str, Any]]:
        return self.data["machines"]

    @property
    def session_defaults(self) -> dict[str, Any]:
        return self.data.get("sessionDefaults", {"models": {}})
