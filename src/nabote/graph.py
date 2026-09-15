"""Agregação em janelas e métricas de grafo.

Separação de responsabilidades que importa:

  `edge_window` guarda CONTAGEM, não peso interpretado. A ponderação por tipo
  de interação é aplicada na hora de montar o grafo. Assim, mudar a opinião
  sobre quanto vale uma citação não obriga a reagregar nada.

Duas visões, nunca misturadas por padrão (plano, frente 07):

  amp    repost + citação — sinal de alinhamento e amplificação
  reply  respostas — frequentemente o oposto de concordância

Misturar as duas produz comunidades sem sentido, porque quem mais responde um
ator costuma ser quem mais discorda dele. `all` existe para quando a pergunta
for sobre atenção total, não sobre alinhamento.

Gramática de escopo:
  edge_window.scope     'all' | 'topic:<id>'          — QUAIS posts
  actor_metric.scope    '<view>' | '<view>:topic:<id>' — QUAL visão sobre eles
"""

from __future__ import annotations

import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import utcnow

GRAPH_VERSION = 1

# Peso relativo por tipo de interação, aplicado na montagem do grafo.
VIEWS: dict[str, dict[str, float]] = {
    "amp": {"repost": 1.0, "quote": 0.8},
    "reply": {"reply": 1.0},
    "all": {"repost": 1.0, "quote": 0.8, "reply": 0.4, "mention": 0.3},
}
DEFAULT_VIEW = "amp"

# Betweenness é O(V·E). Acima disto o cálculo deixa de ser instantâneo e passa
# a ser opcional — o plano prevê graph-tool como saída, não banco de grafos.
BETWEENNESS_MAX_NODES = 20_000

# O Leiden é heurístico e usa aleatoriedade: rodar duas vezes sobre o MESMO
# grafo devolve partições diferentes, e portanto números de comunidade
# diferentes. Sem semente fixa, "comunidade #197" não quer dizer nada de uma
# execução para a outra — e qualquer relatório que cite um número vira ficção.
LEIDEN_SEED = 20260914

# Rodadas do modelo nulo do E-I. Vinte já estabiliza a média em grafos deste
# tamanho; o custo é linear e o `analyze --null 0` desliga.
NULL_TRIALS = 20

# Comparação por faixa de tamanho: uma comunidade só se compara com nulas entre
# metade e o dobro do seu tamanho. Tamanho é distribuído em ordens de grandeza,
# então a faixa é multiplicativa, não aditiva.
SIZE_BAND = 2.0
MIN_NULL_SAMPLES = 5

# Componente com até três atores não é comunidade: é resíduo de amostragem.
# Uma díade solta tem E-I obrigatoriamente −1 porque não existe aresta externa
# possível — o número parece "câmara de eco fechada" e não significa nada.
TRIVIAL_COMPONENT = 3


def window_start_for(timestamp: str) -> str:
    """Segunda-feira da semana do timestamp, como 'YYYY-MM-DD'.

    Semana fechada em segunda é convenção arbitrária, mas precisa ser a MESMA
    em toda parte: janela inconsistente faz série temporal comparar coisas
    diferentes sem avisar.
    """
    text = timestamp.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = datetime.fromisoformat(text[:19])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


# A MESMA regra de `window_start_for`, em SQL, para agregações que não cabem em
# Python. Duplicar a definição é risco real de divergência silenciosa, então
# existe teste comparando as duas para todo dia de vários meses.
WINDOW_SQL = ("date(substr({col},1,10), '-' || "
              "((CAST(strftime('%w', substr({col},1,10)) AS INTEGER) + 6) % 7)"
              " || ' days')")


def windows_present(conn: sqlite3.Connection) -> list[str]:
    """Janelas que têm interação registrada, da mais antiga para a mais nova."""
    rows = conn.execute("SELECT occurred_at FROM interaction").fetchall()
    return sorted({window_start_for(r["occurred_at"]) for r in rows})


def aggregate_window(
    conn: sqlite3.Connection, window_start: str, scope: str = "all"
) -> int:
    """Materializa `edge_window` a partir de `interaction`. Devolve nº de arestas.

    Recomputa a janela inteira: `interaction` é derivada e a agregação também,
    então reprocessar é sempre seguro — princípio 2 do schema.
    """
    end = (datetime.fromisoformat(window_start) + timedelta(days=7)).date().isoformat()
    conn.execute(
        "DELETE FROM edge_window WHERE window_start = ? AND scope = ?",
        (window_start, scope),
    )
    cur = conn.execute(
        """
        INSERT INTO edge_window (window_start, scope, src_actor_id, dst_actor_id, kind, weight)
        SELECT ?, ?, src_actor_id, dst_actor_id, kind, COUNT(*) * 1.0
        FROM interaction
        WHERE occurred_at >= ? AND occurred_at < ?
        GROUP BY src_actor_id, dst_actor_id, kind
        """,
        (window_start, scope, window_start, end),
    )
    return cur.rowcount


def load_graph(
    conn: sqlite3.Connection, window_start: str, view: str = DEFAULT_VIEW,
    edge_scope: str = "all",
) -> tuple[Any, list[int]]:
    """Monta o igraph da janela. Devolve (grafo, actor_ids por índice)."""
    import igraph

    weights = VIEWS[view]
    placeholders = ",".join("?" * len(weights))
    rows = conn.execute(
        f"""
        SELECT src_actor_id, dst_actor_id, kind, weight
        FROM edge_window
        WHERE window_start = ? AND scope = ? AND kind IN ({placeholders})
        """,
        (window_start, edge_scope, *weights.keys()),
    ).fetchall()

    combined: dict[tuple[int, int], float] = {}
    for row in rows:
        key = (row["src_actor_id"], row["dst_actor_id"])
        combined[key] = combined.get(key, 0.0) + row["weight"] * weights[row["kind"]]

    actor_ids = sorted({a for pair in combined for a in pair})
    index = {actor_id: i for i, actor_id in enumerate(actor_ids)}

    graph = igraph.Graph(directed=True)
    graph.add_vertices(len(actor_ids))
    if combined:
        graph.add_edges([(index[s], index[d]) for s, d in combined])
        graph.es["weight"] = list(combined.values())
    return graph, actor_ids


def component_stats(graph: Any) -> dict[str, Any]:
    """Fragmentação do grafo, medida em componentes fracamente conexos.

    Contar comunidades sozinho engana. O Leiden não junta o que o grafo separou:
    cada componente isolado vira pelo menos uma comunidade. Numa coleta por
    termo, a maioria dos atores aparece uma vez só e sai como díade solta —
    centenas de "comunidades" que são ruído de amostragem, não estrutura.

    `core_share` é a métrica de saúde: fração dos atores dentro do maior
    componente. Perto de 1 existe uma rede. Perto de 0 existe uma pilha de
    cacos, e toda métrica relacional calculada em cima dela é local demais
    para significar alguma coisa.
    """
    if graph.vcount() == 0:
        return {"components": 0, "largest": 0, "core_share": 0.0, "trivial": 0}
    sizes = graph.connected_components(mode="weak").sizes()
    largest = max(sizes)
    return {
        "components": len(sizes),
        "largest": largest,
        "core_share": largest / graph.vcount(),
        "trivial": sum(1 for size in sizes if size <= TRIVIAL_COMPONENT),
    }


@contextmanager
def _rng(seed: int = LEIDEN_SEED):
    """Aleatoriedade determinística só durante o Leiden.

    Mexer no `random` global do processo seria grosseiro — outra parte do
    programa pode depender dele. O igraph aceita um gerador próprio, então
    trocamos, usamos e devolvemos.
    """
    import igraph

    igraph.set_random_number_generator(random.Random(seed))
    try:
        yield
    finally:
        igraph.set_random_number_generator(random)


def _relabel_by_size(membership: list[int]) -> list[int]:
    """Renumera comunidades por tamanho: #0 é a maior.

    O rótulo que o Leiden devolve é arbitrário. Ordenar por tamanho faz o
    número carregar significado — "#0" é sempre a maior comunidade da janela —
    e mantém a numeração estável quando só a ordem interna do algoritmo muda.
    Empate é desfeito pelo menor índice de nó, para não reintroduzir acaso.

    Isto NÃO resolve identidade entre janelas: comunidade que cresce ou encolhe
    troca de posição. Rastrear a mesma comunidade ao longo do tempo é
    casamento por sobreposição de membros, e é outro problema.
    """
    grupos: dict[int, list[int]] = {}
    for node, community in enumerate(membership):
        grupos.setdefault(community, []).append(node)
    ordem = sorted(grupos.items(), key=lambda kv: (-len(kv[1]), kv[1][0]))
    novo = {antigo: i for i, (antigo, _) in enumerate(ordem)}
    return [novo[c] for c in membership]


def detect_communities(graph: Any) -> list[int]:
    """Leiden sobre a versão não-dirigida do grafo, renumerado por tamanho.

    Detecção de comunidade por modularidade é definida para grafos não
    dirigidos; a conversão soma os pesos das duas direções. Isso é padrão e
    deliberado — não um descuido com a direção das arestas, que continua
    valendo para PageRank e in-degree.
    """
    if graph.vcount() == 0:
        return []
    if graph.ecount() == 0:
        return list(range(graph.vcount()))
    undirected = graph.as_undirected(combine_edges="sum")
    with _rng():
        clusters = undirected.community_leiden(
            objective_function="modularity", weights="weight"
        )
    return _relabel_by_size(list(clusters.membership))


def ei_index(graph: Any, membership: list[int]) -> list[float]:
    """Índice E-I ponderado por nó: (externas − internas) / total.

    −1 = todas as arestas dentro da própria comunidade (câmara de eco fechada)
    +1 = todas para fora. Nó isolado devolve 0,0 por convenção.
    """
    internal = [0.0] * graph.vcount()
    external = [0.0] * graph.vcount()
    for edge in graph.es:
        src, dst = edge.tuple
        weight = edge["weight"]
        bucket = internal if membership[src] == membership[dst] else external
        bucket[src] += weight
        bucket[dst] += weight
    out = []
    for i in range(graph.vcount()):
        total = internal[i] + external[i]
        out.append(0.0 if total == 0 else (external[i] - internal[i]) / total)
    return out


def community_ei(graph: Any, membership: list[int]) -> dict[int, float]:
    """E-I médio de cada comunidade."""
    ei = ei_index(graph, membership)
    soma: dict[int, list[float]] = {}
    for node, community in enumerate(membership):
        soma.setdefault(int(community), []).append(ei[node])
    return {c: sum(v) / len(v) for c, v in soma.items()}


def ei_null_model(graph: Any, membership: list[int], trials: int = NULL_TRIALS,
                  seed: int = LEIDEN_SEED) -> dict[int, tuple[float, float]]:
    """E-I esperado para uma comunidade DAQUELE TAMANHO, achada por acaso.

    O problema que isto resolve: o E-I depende do tamanho da comunidade. Uma
    comunidade maior captura mais arestas dentro de si por combinatória e parece
    mais fechada sem ninguém ter se comportado de modo diferente. No dado real,
    tamanho e E-I correlacionam −0,73 DENTRO de uma única janela — não é efeito
    da densidade da coleta, é do tamanho. Sem correção, comparar o E-I de duas
    comunidades compara principalmente o tamanho delas.

    O nulo certo custou duas tentativas. Embaralhar as arestas e MANTER a
    partição não serve: a partição foi ajustada a este grafo, então vence
    qualquer embaralhamento dele por construção — um grafo aleatório dava z de
    −9, quando deveria dar ~0. O nulo tem de refazer também a detecção.

    Então: embaralha preservando o grau de cada nó (modelo de configuração),
    roda o Leiden de novo no grafo embaralhado e guarda os pares (tamanho, E-I)
    que aparecem. A comunidade observada é comparada com as comunidades NULAS DE
    TAMANHO PARECIDO — apples to apples.

    Devolve {comunidade: (media_nula, desvio)}, ausente quando não houve
    comunidade nula de tamanho comparável.

      z ≈ 0    fechamento igual ao que o acaso produz nesse tamanho: sem achado
      z ≪ 0    fechada ALÉM do que tamanho e graus explicam: câmara de eco

    Limite conhecido: `rewire` preserva o grau NÃO ponderado e o conjunto de
    pesos, não a força ponderada de cada nó. Na visão `amp`, onde quase todo
    peso é repostagem de peso 1, a diferença é pequena.
    """
    import random as _random

    if graph.ecount() == 0:
        return {}

    tamanhos: dict[int, int] = {}
    for community in membership:
        tamanhos[int(community)] = tamanhos.get(int(community), 0) + 1

    pesos = list(graph.es["weight"])
    rng = _random.Random(seed)
    nulas: list[tuple[int, float]] = []
    for _ in range(trials):
        sorteado = graph.copy()
        with _rng(rng.randrange(2**31)):
            # 10 trocas por aresta descorrelaciona sem custo absurdo.
            sorteado.rewire(n=10 * sorteado.ecount(), mode="simple")
        # `rewire` perde os atributos. Os pesos voltam embaralhados: o nulo
        # preserva a DISTRIBUIÇÃO de peso, não a associação entre peso e par —
        # que é justamente o que se quer destruir.
        embaralhados = pesos[:]
        rng.shuffle(embaralhados)
        sorteado.es["weight"] = embaralhados

        nula = detect_communities(sorteado)
        conta: dict[int, int] = {}
        for community in nula:
            conta[community] = conta.get(community, 0) + 1
        for community, valor in community_ei(sorteado, nula).items():
            nulas.append((conta[community], valor))

    if not nulas:
        return {}

    saida: dict[int, tuple[float, float]] = {}
    for community, tamanho in tamanhos.items():
        # Faixa multiplicativa: comunidade de 5.000 não se compara com uma de
        # 3, e tamanho é distribuído em ordens de grandeza, não linearmente.
        perto = [ei for n, ei in nulas
                 if tamanho / SIZE_BAND <= n <= tamanho * SIZE_BAND]
        if len(perto) < MIN_NULL_SAMPLES:
            continue
        media = sum(perto) / len(perto)
        variancia = sum((v - media) ** 2 for v in perto) / len(perto)
        saida[community] = (media, variancia ** 0.5)
    return saida


def compute_metrics(graph: Any, membership: list[int]) -> dict[str, list[float]]:
    """Métricas do MVP. Todas em segundos no volume previsto."""
    if graph.vcount() == 0:
        return {}

    metrics: dict[str, list[float]] = {
        "in_degree_w": graph.strength(mode="in", weights="weight"),
        "out_degree_w": graph.strength(mode="out", weights="weight"),
        "pagerank": graph.pagerank(weights="weight", directed=True),
        "ei_index": ei_index(graph, membership),
    }

    if graph.vcount() <= BETWEENNESS_MAX_NODES and graph.ecount():
        # Betweenness trata peso como DISTÂNCIA: aresta forte precisa ser
        # caminho CURTO. Sem inverter, o cálculo sai com o sentido trocado —
        # e o resultado parece plausível, que é o pior tipo de erro.
        distances = [1.0 / w if w else 1.0 for w in graph.es["weight"]]
        metrics["betweenness"] = graph.betweenness(weights=distances, directed=True)

    return metrics


def analyze_window(
    conn: sqlite3.Connection, window_start: str, view: str = DEFAULT_VIEW,
    edge_scope: str = "all", graph_version: int = GRAPH_VERSION,
    null_trials: int = NULL_TRIALS,
) -> dict[str, Any]:
    """Calcula e grava métricas e comunidades de uma janela."""
    scope = view if edge_scope == "all" else f"{view}:{edge_scope}"
    graph, actor_ids = load_graph(conn, window_start, view, edge_scope)

    if graph.vcount() == 0:
        return {"window_start": window_start, "scope": scope, "nodes": 0,
                "edges": 0, "communities": 0, **component_stats(graph)}

    membership = detect_communities(graph)
    metrics = compute_metrics(graph, membership)

    for table in ("actor_metric", "community", "actor_community"):
        conn.execute(
            f"DELETE FROM {table} WHERE window_start = ? AND scope = ? AND graph_version = ?",
            (window_start, scope, graph_version),
        )

    conn.executemany(
        "INSERT INTO actor_metric (actor_id, window_start, scope, metric, value, graph_version) "
        "VALUES (?,?,?,?,?,?)",
        [(actor_ids[i], window_start, scope, name, float(values[i]), graph_version)
         for name, values in metrics.items() for i in range(len(actor_ids))],
    )
    conn.executemany(
        "INSERT INTO actor_community (actor_id, window_start, scope, community_id, graph_version) "
        "VALUES (?,?,?,?,?)",
        [(actor_ids[i], window_start, scope, int(membership[i]), graph_version)
         for i in range(len(actor_ids))],
    )

    ei = metrics.get("ei_index", [])
    sizes: dict[int, list[int]] = {}
    for i, community_id in enumerate(membership):
        sizes.setdefault(int(community_id), []).append(i)

    nulo = ei_null_model(graph, membership, trials=null_trials) if null_trials else {}

    def _linha(cid: int, members: list[int]) -> tuple:
        media = (sum(ei[i] for i in members) / len(members)) if ei else None
        esperado, desvio = nulo.get(cid, (None, None))
        z = None
        if media is not None and esperado is not None and desvio:
            z = (media - esperado) / desvio
        return (window_start, scope, cid, graph_version, len(members), media,
                esperado, z, null_trials if esperado is not None else None)

    conn.executemany(
        "INSERT INTO community (window_start, scope, community_id, graph_version, "
        "size, ei_mean, ei_null, ei_z, null_trials) VALUES (?,?,?,?,?,?,?,?,?)",
        [_linha(cid, members) for cid, members in sizes.items()],
    )

    conn.execute(
        "UPDATE actor_community SET membership = 1.0 WHERE window_start = ? AND scope = ?",
        (window_start, scope),
    )

    return {
        "window_start": window_start, "scope": scope,
        "nodes": graph.vcount(), "edges": graph.ecount(),
        "communities": len(sizes), "metrics": sorted(metrics),
        **component_stats(graph),
        "computed_at": utcnow(),
    }
