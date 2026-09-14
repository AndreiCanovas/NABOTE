"""Fonte de arquivo JSONL — testes e replay.

Sem ela o parser só seria exercitável com rede, o que significa que não seria
exercitável em CI nem de forma determinística.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator


class FixtureSource:
    def __init__(self, path: Path | str, name: str = "fixture") -> None:
        self.path = Path(path)
        self.name = name

    def events(self, cursor: str | None = None) -> Iterator[dict[str, Any]]:
        after = int(cursor) if cursor else None
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                event = json.loads(line)
                if after is not None and event.get("time_us", 0) <= after:
                    continue
                yield event
