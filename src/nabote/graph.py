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

import sqlite3
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


def detect_communities(graph: Any) -> list[int]:
    """Leiden sobre a versão não-dirigida do grafo.

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
    clusters = undirected.community_leiden(
        objective_function="modularity", weights="weight"
    )
    return list(clusters.membership)


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
    conn.executemany(
        "INSERT INTO community (window_start, scope, community_id, graph_version, size, ei_mean) "
        "VALUES (?,?,?,?,?,?)",
        [(window_start, scope, cid, graph_version, len(members),
          (sum(ei[i] for i in members) / len(members)) if ei else None)
         for cid, members in sizes.items()],
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
