"""Os números do Radar de Pautas, num objeto só.

O relatório recorrente pede uma dúzia de recortes que não existem em nenhuma
tabela: volume por pauta, quantas comunidades cada pauta atravessa, quem é
central DENTRO dela, o que mudou desde a janela anterior. Cada um é uma consulta
diferente, e reunir isso à mão toda semana é como o relatório deixa de ser
recorrente.

Duas decisões que valem registro:

A pauta aqui é o rótulo da coleta — o Trending Topic que originou o arquivo.
É grosso, e é independente da estrutura que definiu as comunidades: a comunidade
foi descoberta só pelo grafo, e o assunto entra depois. Em produção a pauta virá
do agrupamento de texto; esta função troca uma consulta e o resto continua.

"Quantas comunidades a pauta atravessa" conta comunidades do grafo CHEIO que
guardam pelo menos `SPREAD_FLOOR` dos atores da pauta, e não comunidades do
grafo da própria pauta. A pergunta é se a pauta circula entre grupos que já
existem — não como ela se subdivide por dentro.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .graph import (GRAPH_VERSION, TOPIC_PREFIX, WINDOW_SQL,
                    topic_scope, topics_in_window)

# Abaixo disto a presença da pauta numa comunidade é respingo, não circulação.
SPREAD_FLOOR = 0.05
# Quantas contas contam como "poucas contas puxando muito volume".
CONCENTRATION_TOP = 3
TOP_ACTORS = 5


def _janela_fim(window_start: str) -> str:
    return (datetime.fromisoformat(window_start) + timedelta(days=7)).date().isoformat()


def topic_volume(conn: sqlite3.Connection, window_start: str) -> dict[str, dict[str, Any]]:
    """Posts, autores e concentração de cada pauta na janela."""
    fim = _janela_fim(window_start)
    saida: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        """
        SELECT cr.campaign_label AS t, COUNT(*) AS posts,
               COUNT(DISTINCT p.actor_id) AS autores
        FROM post p JOIN collection_run cr ON cr.run_id = p.run_id
        WHERE p.created_at >= ? AND p.created_at < ? AND cr.campaign_label IS NOT NULL
        GROUP BY cr.campaign_label
        """, (window_start, fim)
    ):
        saida[r["t"]] = {"posts": r["posts"], "autores": r["autores"]}

    # concentração: fatia dos posts nas mãos das N maiores contas da pauta
    for termo, dados in saida.items():
        topo = [r["n"] for r in conn.execute(
            """
            SELECT COUNT(*) AS n FROM post p
            JOIN collection_run cr ON cr.run_id = p.run_id
            WHERE p.created_at >= ? AND p.created_at < ? AND cr.campaign_label = ?
            GROUP BY p.actor_id ORDER BY n DESC LIMIT ?
            """, (window_start, fim, termo, CONCENTRATION_TOP))]
        dados["concentracao"] = sum(topo) / dados["posts"] if dados["posts"] else 0.0
    return saida


def topic_spread(conn: sqlite3.Connection, window_start: str, base_scope: str,
                 graph_version: int = GRAPH_VERSION) -> dict[str, dict[str, Any]]:
    """Por quais comunidades do grafo cheio cada pauta circula.

    Atores da pauta que não estão no escopo base ficam de fora e são contados
    em `fora`: numa pauta onde a maioria ficou de fora, "comunidade dominante"
    descreve uma minoria e precisa ser lida com reserva.
    """
    fim = _janela_fim(window_start)
    saida: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
        """
        SELECT cr.campaign_label AS t, ac.community_id AS com,
               COUNT(DISTINCT p.actor_id) AS n
        FROM post p
        JOIN collection_run cr ON cr.run_id = p.run_id
        LEFT JOIN actor_community ac ON ac.actor_id = p.actor_id
             AND ac.window_start = ? AND ac.scope = ? AND ac.graph_version = ?
        WHERE p.created_at >= ? AND p.created_at < ? AND cr.campaign_label IS NOT NULL
        GROUP BY cr.campaign_label, ac.community_id
        """, (window_start, base_scope, graph_version, window_start, fim)
    ):
        alvo = saida.setdefault(r["t"], {"por_comunidade": {}, "fora": 0})
        if r["com"] is None:
            alvo["fora"] = r["n"]
        else:
            alvo["por_comunidade"][r["com"]] = r["n"]

    for dados in saida.values():
        dentro = sum(dados["por_comunidade"].values())
        dados["atores_no_escopo"] = dentro
        ordenado = sorted(dados["por_comunidade"].items(), key=lambda kv: -kv[1])
        dados["dominante"] = ordenado[0][0] if ordenado else None
        dados["fatia_dominante"] = (ordenado[0][1] / dentro) if dentro and ordenado else 0.0
        dados["atravessa"] = sum(1 for _, n in ordenado if dentro and n / dentro >= SPREAD_FLOOR)
    return saida


def topic_actors(conn: sqlite3.Connection, window_start: str, topico: str,
                 view: str, graph_version: int = GRAPH_VERSION) -> list[dict[str, Any]]:
    """Os atores mais centrais DENTRO da pauta, pelo PageRank do escopo dela.

    É o ponto em que o escopo importa: o mesmo perfil aparece em posições muito
    diferentes conforme a pauta, e uma métrica global de influência esconderia
    exatamente esse comportamento.
    """
    scope = f"{view}:{topic_scope([topico])}"
    return [dict(r) for r in conn.execute(
        """
        SELECT COALESCE(a.handle, a.platform_user_id) AS quem,
               MAX(CASE WHEN m.metric='pagerank'    THEN m.value END) AS pagerank,
               MAX(CASE WHEN m.metric='in_degree_w' THEN m.value END) AS in_degree
        FROM actor_metric m JOIN actor a ON a.actor_id = m.actor_id
        WHERE m.window_start=? AND m.scope=? AND m.graph_version=?
        GROUP BY a.actor_id ORDER BY pagerank DESC LIMIT ?
        """, (window_start, scope, graph_version, TOP_ACTORS))]


def community_rows(conn: sqlite3.Connection, window_start: str, scope: str,
                   termos_por_comunidade: int = 3,
                   graph_version: int = GRAPH_VERSION) -> list[dict[str, Any]]:
    """Comunidades com tamanho, fechamento, fatia de volume e pautas."""
    janela_sql = WINDOW_SQL.format(col="p.created_at")
    pautas: dict[int, list[tuple[str, int]]] = {}
    volume: dict[int, int] = {}
    for r in conn.execute(
        f"""
        SELECT ac.community_id AS com, cr.campaign_label AS termo, COUNT(*) AS posts
        FROM actor_community ac
        JOIN post p ON p.actor_id = ac.actor_id
        JOIN collection_run cr ON cr.run_id = p.run_id
        WHERE ac.window_start=? AND ac.scope=? AND ac.graph_version=? AND {janela_sql} = ?
        GROUP BY ac.community_id, cr.campaign_label
        """, (window_start, scope, graph_version, window_start)
    ):
        pautas.setdefault(r["com"], []).append((r["termo"] or "?", r["posts"]))
        volume[r["com"]] = volume.get(r["com"], 0) + r["posts"]

    total = sum(volume.values()) or 1
    saida = []
    for r in conn.execute(
        "SELECT community_id, size, ei_mean, ei_choice, choice_actors FROM community "
        "WHERE window_start=? AND scope=? AND graph_version=? ORDER BY size DESC",
        (window_start, scope, graph_version)
    ):
        com = r["community_id"]
        lista = sorted(pautas.get(com, []), key=lambda t: -t[1])[:termos_por_comunidade]
        saida.append({
            "id": com, "atores": r["size"], "ei": r["ei_choice"], "ei_bruto": r["ei_mean"],
            "n_escolha": r["choice_actors"],
            "volume": volume.get(com, 0), "fatia_volume": volume.get(com, 0) / total,
            "pautas": [{"termo": t, "posts": n} for t, n in lista],
        })
    return saida


def snapshot(conn: sqlite3.Connection, window_start: str, view: str = "amp",
             base_scope: str | None = None,
             graph_version: int = GRAPH_VERSION) -> dict[str, Any]:
    """Tudo que o Radar precisa de uma janela, num objeto só."""
    base_scope = base_scope or f"{view}:core"
    volumes = topic_volume(conn, window_start)
    spread = topic_spread(conn, window_start, base_scope, graph_version)

    pautas = []
    for termo in topics_in_window(conn, window_start):
        v, s = volumes.get(termo, {}), spread.get(termo, {})
        pautas.append({
            "termo": termo,
            "posts": v.get("posts", 0),
            "autores": v.get("autores", 0),
            "concentracao": round(v.get("concentracao", 0.0), 3),
            "atravessa": s.get("atravessa", 0),
            "dominante": s.get("dominante"),
            "fatia_dominante": round(s.get("fatia_dominante", 0.0), 3),
            "fora_do_nucleo": s.get("fora", 0),
            "atores": topic_actors(conn, window_start, termo, view, graph_version),
        })

    graf = conn.execute(
        "SELECT COUNT(DISTINCT actor_id) AS n FROM actor_metric "
        "WHERE window_start=? AND scope=?", (window_start, view)).fetchone()["n"]
    custo = conn.execute(
        "SELECT COALESCE(SUM(cost_usd),0) AS c FROM collection_run cr "
        "WHERE EXISTS (SELECT 1 FROM post p WHERE p.run_id = cr.run_id "
        f"AND {WINDOW_SQL.format(col='p.created_at')} = ?)", (window_start,)
    ).fetchone()["c"]

    return {
        "janela": window_start,
        "view": view,
        "escopo_base": base_scope,
        "atores_grafo": graf,
        "custo_usd": round(custo, 4),
        "pautas": pautas,
        "comunidades": community_rows(conn, window_start, base_scope,
                                      graph_version=graph_version),
    }
