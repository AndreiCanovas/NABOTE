"""Gerador de eventos sintéticos com estrutura de comunidade PLANTADA.

A ideia do teste: se a gente sabe qual é a estrutura de verdade, dá para
verificar se o pipeline a recupera. Um grafo aleatório não prova nada; um grafo
com comunidades plantadas prova que agregação, pesos e Leiden funcionam juntos.

Tudo aqui é inventado e determinístico por semente. Nenhum dado real.
"""

from __future__ import annotations

import random
from typing import Any

BASE_TIME_US = 1_757_000_000_000_000  # dentro de uma única janela semanal
DAY_US = 86_400_000_000


def did(community: int, index: int) -> str:
    return f"did:plc:sint{community:02d}{index:03d}"


def _commit(actor: str, time_us: int, collection: str, rkey: str,
            record: dict[str, Any]) -> dict[str, Any]:
    return {
        "did": actor, "time_us": time_us, "kind": "commit",
        "commit": {"rev": f"rev{rkey}", "operation": "create",
                   "collection": collection, "rkey": rkey,
                   "cid": f"bafy{rkey}", "record": record},
    }


def _repost(actor: str, target: str, time_us: int, rkey: str) -> dict[str, Any]:
    return _commit(actor, time_us, "app.bsky.feed.repost", rkey, {
        "$type": "app.bsky.feed.repost",
        "createdAt": "2026-09-15T12:00:00.000Z",
        "subject": {"uri": f"at://{target}/app.bsky.feed.post/orig{rkey}",
                    "cid": f"bafyo{rkey}"}})


def _reply(actor: str, target: str, time_us: int, rkey: str) -> dict[str, Any]:
    return _commit(actor, time_us, "app.bsky.feed.post", rkey, {
        "$type": "app.bsky.feed.post", "text": "resposta sintética",
        "createdAt": "2026-09-15T12:00:00.000Z", "langs": ["pt"],
        "reply": {"root": {"uri": f"at://{target}/app.bsky.feed.post/r{rkey}"},
                  "parent": {"uri": f"at://{target}/app.bsky.feed.post/r{rkey}"}}})


def planted_communities(
    n_communities: int = 3, per_community: int = 8, within_edges: int = 5,
    bridges: int = 2, seed: int = 7,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Eventos + o gabarito {did: comunidade_verdadeira}.

    A estrutura vive nos REPOSTS (visão `amp`). As respostas são plantadas
    CRUZANDO comunidades de propósito: no mundo real quem mais responde um ator
    costuma ser quem mais discorda dele. Se a visão `amp` for contaminada por
    respostas, o teste de recuperação quebra — que é exatamente o ponto.
    """
    rng = random.Random(seed)
    events: list[dict[str, Any]] = []
    truth: dict[str, int] = {}
    clock = BASE_TIME_US
    counter = 0

    for community in range(n_communities):
        for index in range(per_community):
            truth[did(community, index)] = community

    for community in range(n_communities):
        members = [did(community, i) for i in range(per_community)]
        hub = members[0]
        for actor in members[1:]:
            for _ in range(within_edges):
                # o hub concentra amplificação, como num cluster real
                target = hub if rng.random() < 0.55 else rng.choice(
                    [m for m in members if m != actor])
                counter += 1
                clock += 1_000
                events.append(_repost(actor, target, clock, f"p{counter:05d}"))

    for _ in range(bridges):
        left, right = rng.sample(range(n_communities), 2)
        counter += 1
        clock += 1_000
        events.append(_repost(did(left, rng.randrange(per_community)),
                              did(right, 0), clock, f"p{counter:05d}"))

    for community in range(n_communities):
        other = (community + 1) % n_communities
        for index in range(1, min(4, per_community)):
            counter += 1
            clock += 1_000
            events.append(_reply(did(community, index), did(other, 0),
                                 clock, f"p{counter:05d}"))

    return events, truth


def write_jsonl(events: list[dict[str, Any]], path) -> None:
    import json
    from pathlib import Path
    Path(path).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")


class ListSource:
    """Fonte em memória, para não precisar de arquivo temporário nos testes."""

    name = "sintetico"

    def __init__(self, events: list[dict[str, Any]], name: str = "sintetico") -> None:
        self._events = events
        self.name = name
        self.skipped = 0

    def events(self, cursor: str | None = None):
        from nabote import atproto

        after = int(cursor) if cursor else None
        for evento in self._events:
            if after is not None and evento.get("time_us", 0) <= after:
                continue
            ev = atproto.normalize(evento)
            if ev is None:
                self.skipped += 1
                continue
            yield ev
