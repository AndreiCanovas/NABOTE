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


def scattered_dyads(n: int = 40, seed: int = 11) -> list[dict[str, Any]]:
    """Pares soltos: dois atores, uma aresta, zero ligação com o resto do grafo.

    É o que a coleta por TERMO produz em massa — a maioria dos usuários aparece
    uma vez só na amostra. Cada par vira um componente isolado e portanto uma
    "comunidade" com E-I −1 que não informa nada. Sem isto plantado, nenhum
    teste enxerga o problema que a base real do X exibiu de cara.
    """
    rng = random.Random(seed)
    events: list[dict[str, Any]] = []
    clock = BASE_TIME_US + DAY_US
    for i in range(n):
        clock += 1_000
        events.append(_repost(f"did:plc:solto{i:04d}a", f"did:plc:solto{i:04d}b",
                              clock, f"d{i:05d}{rng.randrange(10)}"))
    return events


def ambiguous_graph(seed: int = 3, blocks: int = 12, per_block: int = 10,
                    p_in: float = 0.28, p_out: float = 0.035):
    """Grafo igraph com estrutura AMBÍGUA de propósito, construído determinístico.

    Blocos densos ligados por ruído suficiente para o Leiden hesitar: sem
    semente fixa, doze execuções devolvem doze partições diferentes. É o único
    jeito de um teste de determinismo valer alguma coisa — no grafo plantado
    limpo o Leiden acerta sempre, com ou sem semente, e o teste passa vazio.
    """
    import igraph

    rng = random.Random(seed)
    n = blocks * per_block
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            probability = p_in if i // per_block == j // per_block else p_out
            if rng.random() < probability:
                edges.append((i, j))
    g = igraph.Graph(directed=True)
    g.add_vertices(n)
    g.add_edges(edges)
    g.es["weight"] = [1.0] * len(edges)
    return g


def block_graph(sizes: list[int], p_in: float = 0.5, p_out: float = 0.004,
                seed: int = 2):
    """Grafo de blocos densos com TAMANHOS desiguais, determinístico.

    Serve para checar o pareamento por tamanho do modelo nulo: com blocos de 100,
    30 e 10 o nulo tem de dar valores diferentes para cada faixa, porque o
    próprio acaso produz E-I diferente conforme o tamanho.
    """
    import igraph

    rng = random.Random(seed)
    rotulos = [b for b, t in enumerate(sizes) for _ in range(t)]
    n = len(rotulos)
    edges = [(i, j) for i in range(n) for j in range(i + 1, n)
             if rng.random() < (p_in if rotulos[i] == rotulos[j] else p_out)]
    g = igraph.Graph(directed=True)
    g.add_vertices(n)
    g.add_edges(edges)
    g.es["weight"] = [1.0] * len(edges)
    return g


def hub_audience_graph(sizes: list[int], external_fraction: float = 0.05,
                       seed: int = 3):
    """Rede de audiência-em-torno-de-hub — a forma do dado real.

    Cada hub tem uma audiência de grau 1 e manda arestas externas PROPORCIONAIS
    à própria audiência, sorteadas entre os outros hubs com peso pelo tamanho
    deles. Ou seja: comportamento relativo idêntico, tamanhos muito diferentes.

    É o banco de provas do E-I. Uma métrica de fechamento que varia com o
    tamanho neste grafo está medindo tamanho, não fechamento.
    """
    import igraph

    rng = random.Random(seed)
    edges: list[tuple[int, int]] = []
    proximo = 0
    hubs: list[int] = []
    tamanho: dict[int, int] = {}
    for size in sizes:
        hub = proximo
        proximo += 1
        hubs.append(hub)
        tamanho[hub] = size
        for _ in range(size):
            edges.append((proximo, hub))
            proximo += 1
    for hub in hubs:
        outros = [h for h in hubs if h != hub]
        pesos = [tamanho[h] for h in outros]
        for _ in range(int(tamanho[hub] * external_fraction)):
            edges.append((hub, rng.choices(outros, weights=pesos)[0]))
    g = igraph.Graph(directed=True)
    g.add_vertices(proximo)
    g.add_edges(edges)
    g.es["weight"] = [1.0] * len(edges)
    return g


def extra_replies(community: int, target_community: int, indices: list[int],
                  tag: str = "x") -> list[dict[str, Any]]:
    """Respostas adicionais de uma comunidade para outra.

    Serve para DESBALANCEAR os tamanhos no grafo de respostas. Com comunidades
    de tamanhos iguais, renumerar por tamanho é a identidade, e um teste de
    preservação de numeração passa sem testar nada.
    """
    events = []
    clock = BASE_TIME_US + 2 * DAY_US
    for k, index in enumerate(indices):
        clock += 1_000
        events.append(_reply(did(community, index), did(target_community, 0),
                             clock, f"{tag}{k:05d}"))
    return events


def single_voice_graph(outros: int = 200, do_dominante: int = 400, seed: int = 4):
    """Grafo onde UM ator responde por quase todo o peso de saída.

    É a forma degenerada que apareceu no grafo de respostas real: uma conta em
    15 das 20 arestas mais pesadas. Métrica de rede ali descreve aquela conta,
    não a rede.
    """
    import igraph

    rng = random.Random(seed)
    n = outros + 2
    edges = [(0, rng.randrange(2, n)) for _ in range(do_dominante)]
    edges += [(rng.randrange(2, n), rng.randrange(2, n)) for _ in range(outros)]
    edges = [(a, b) for a, b in edges if a != b]
    g = igraph.Graph(directed=True)
    g.add_vertices(n)
    g.add_edges(edges)
    g.es["weight"] = [1.0] * len(edges)
    return g


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
