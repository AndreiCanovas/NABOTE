"""Interface que toda fonte precisa cumprir."""

from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class Source(Protocol):
    """Emite eventos crus, no formato do Jetstream.

    O formato do Jetstream é o denominador comum porque é o mais simples dos
    que vamos consumir. Uma fonte de X converte para ele; o resto do pipeline
    não precisa saber da diferença.
    """

    name: str

    def events(self, cursor: str | None = None) -> Iterator[dict[str, Any]]:
        """Itera eventos a partir do cursor. Bloqueante e potencialmente infinito."""
        ...
