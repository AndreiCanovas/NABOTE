"""Fonte de arquivo JSONL no formato do Jetstream — testes e replay.

Sem ela o parser do Bluesky só seria exercitável com rede, o que significa que
não seria exercitável em CI nem de forma determinística.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import atproto
from ..events import NormalizedEvent


class FixtureSource:
    def __init__(self, path: Path | str, name: str = "fixture") -> None:
        self.path = Path(path)
        self.name = name
        self.skipped = 0

    def events(self, cursor: str | None = None) -> Iterator[NormalizedEvent]:
        self.skipped = 0
        after = int(cursor) if cursor else None
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                cru = json.loads(line)
                if after is not None and cru.get("time_us", 0) <= after:
                    continue
                ev = atproto.normalize(cru)
                if ev is None:
                    # o firehose traz curtidas, follows e formatos que não nos
                    # interessam. Contar é operacionalmente útil: saber que 90%
                    # do volume foi descartado explica uma coleta "vazia".
                    self.skipped += 1
                    continue
                yield ev
