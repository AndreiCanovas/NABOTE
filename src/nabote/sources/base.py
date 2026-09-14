"""Interface que toda fonte precisa cumprir."""

from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from ..events import NormalizedEvent


@runtime_checkable
class Source(Protocol):
    """Emite eventos JÁ NORMALIZADOS.

    Cada fonte traduz o seu próprio formato. Nada fora de `sources/` sabe se o
    dado veio de WebSocket, de parquet ou de arquivo — que é o que permite
    trocar de provedor sem tocar no pipeline.
    """

    name: str

    def events(self, cursor: str | None = None) -> Iterator[NormalizedEvent]:
        """Itera eventos a partir do cursor. Pode ser bloqueante e infinito."""
        ...
