"""Evento normalizado — a fronteira entre fonte e pipeline.

Antes, as fontes entregavam o JSON cru do Jetstream e o `ingest` chamava o
normalizador do AT Protocol. Isso funcionava enquanto só existia Bluesky;
com uma segunda plataforma, forçar dado do X no envelope do Bluesky seria
absurdo. Agora cada fonte normaliza o SEU formato e entrega isto aqui, que
não sabe de plataforma nenhuma.

É o que a "camada de fonte abstrata" do plano deveria ter sido desde o começo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tipos de aresta. Iguais nas duas plataformas — a semântica de "amplifiquei",
# "respondi", "citei" e "mencionei" não depende de quem hospeda.
KIND_REPOST = "repost"
KIND_REPLY = "reply"
KIND_QUOTE = "quote"
KIND_MENTION = "mention"
EDGE_KINDS = (KIND_REPOST, KIND_REPLY, KIND_QUOTE, KIND_MENTION)

POST_TYPES = ("original", "repost", "reply", "quote")


@dataclass
class Target:
    """Ponta de destino de uma aresta.

    `uid` é o identificador ESTÁVEL na plataforma; `handle` é o nome legível,
    que muda. Guardar os dois é o que permite o ator Tier C aparecer no
    relatório com nome em vez de número.
    """
    kind: str
    uid: str
    handle: str | None = None
    post_uid: str | None = None


@dataclass
class NormalizedEvent:
    """Um fato observado, já traduzido para o vocabulário do banco."""

    platform: str
    kind: str                       # post | identity | delete
    actor_uid: str
    occurred_at: str                # ISO-8601 UTC
    cursor: str | None = None       # posição para retomada, se a fonte tiver
    actor_handle: str | None = None
    post_uid: str | None = None
    post_type: str | None = None
    text: str | None = None
    lang: str | None = None
    targets: list[Target] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_post(self) -> bool:
        return self.kind == "post"
