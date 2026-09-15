"""Exportação: do banco para arquivos que outra pessoa consegue usar.

Passo 4 do plano. Até aqui tudo vivia no scrollback do terminal, o que tem dois
problemas: some, e não alimenta nada.

Três decisões que moldam este módulo:

CSV, não um formato esperto. Abre no Excel, no pandas, no R, no que a pessoa
tiver. O instrumento é interno; o consumidor é um analista, não um sistema.

Manifesto junto, sempre. Um CSV solto não diz de qual janela veio, de qual
escopo, com qual versão de grafo, nem quantos dias de coleta o alimentaram. Esta
POC gastou três mensagens interpretando dados cuja procedência ninguém tinha
verificado; o manifesto existe para isso não se repetir do lado de fora.

Ressalvas viajam com o dado. O que a amostra NÃO sustenta vai escrito no
manifesto — não no README, não na cabeça de quem exportou. Número sem ressalva
vira slide, e slide não tem rodapé.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import utcnow
from .graph import GRAPH_VERSION, WINDOW_SQL

# Ordem fixa: diff de export entre duas semanas tem de mostrar mudança de dado,
# não de ordem de coluna.
ACTOR_COLUMNS = ["handle", "platform_user_id", "tier", "community",
                 "pagerank", "in_degree_w", "out_degree_w", "ei_index",
                 "betweenness"]


def _escreve(caminho: Path, cabecalho: list[str], linhas) -> int:
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(cabecalho)
        n = 0
        for linha in linhas:
            escritor.writerow(linha)
            n += 1
    return n


def export_actors(conn: sqlite3.Connection, window: str, scope: str,
                  destino: Path, graph_version: int = GRAPH_VERSION) -> int:
    """Um ator por linha, com todas as métricas da janela em colunas."""
    metricas = ",".join(
        f"MAX(CASE WHEN m.metric='{nome}' THEN m.value END) AS {nome}"
        for nome in ACTOR_COLUMNS[4:])
    linhas = conn.execute(f"""
        SELECT a.handle, a.platform_user_id, a.tier, c.community_id, {metricas}
        FROM actor_metric m
        JOIN actor a ON a.actor_id = m.actor_id
        LEFT JOIN actor_community c ON c.actor_id = m.actor_id
             AND c.window_start = m.window_start AND c.scope = m.scope
             AND c.graph_version = m.graph_version
        WHERE m.window_start=? AND m.scope=? AND m.graph_version=?
        GROUP BY a.actor_id
        ORDER BY pagerank DESC
    """, (window, scope, graph_version))
    return _escreve(destino / "actors.csv", ACTOR_COLUMNS, linhas)


def export_communities(conn: sqlite3.Connection, window: str, scope: str,
                       destino: Path, termos_por_comunidade: int = 5,
                       graph_version: int = GRAPH_VERSION) -> int:
    """Uma comunidade por linha, com as pautas dominantes já resolvidas.

    Os termos entram como texto numa coluna só, em vez de uma tabela à parte:
    quem abre este arquivo quer olhar a comunidade e entender do que ela fala,
    não fazer join.
    """
    janela_sql = WINDOW_SQL.format(col="p.created_at")
    pautas: dict[int, list[tuple[str, int]]] = {}
    for r in conn.execute(f"""
        SELECT ac.community_id AS com, cr.campaign_label AS termo, COUNT(*) AS posts
        FROM actor_community ac
        JOIN post p ON p.actor_id = ac.actor_id
        JOIN collection_run cr ON cr.run_id = p.run_id
        WHERE ac.window_start=? AND ac.scope=? AND ac.graph_version=?
              AND {janela_sql} = ?
        GROUP BY ac.community_id, cr.campaign_label
    """, (window, scope, graph_version, window)):
        pautas.setdefault(r["com"], []).append((r["termo"] or "?", r["posts"]))

    def linhas():
        for r in conn.execute(
            "SELECT community_id, size, ei_mean, ei_choice, choice_actors "
            "FROM community WHERE window_start=? AND scope=? AND graph_version=? "
            "ORDER BY size DESC", (window, scope, graph_version)
        ):
            lista = sorted(pautas.get(r["community_id"], []), key=lambda t: -t[1])
            total = sum(n for _, n in lista)
            texto = " · ".join(
                f"{termo} {n / total:.0%}" for termo, n in lista[:termos_por_comunidade]
            ) if total else ""
            yield [r["community_id"], r["size"], r["ei_mean"], r["ei_choice"],
                   r["choice_actors"], total, texto]

    return _escreve(destino / "communities.csv",
                    ["community_id", "size", "ei_mean", "ei_choice",
                     "choice_actors", "posts", "pautas"], linhas())


def export_edges(conn: sqlite3.Connection, window: str, edge_scope: str,
                 destino: Path, kinds: list[str] | None = None,
                 apenas_de: str | None = None,
                 graph_version: int = GRAPH_VERSION) -> int:
    """Uma aresta por linha. `apenas_de` restringe aos atores daquele escopo."""
    filtros = ["e.window_start=?", "e.scope=?"]
    valores: list[Any] = [window, edge_scope]
    if kinds:
        filtros.append(f"e.kind IN ({','.join('?' * len(kinds))})")
        valores += kinds

    # O recorte de escopo é aplicado em Python, não como subconsulta `IN`. Com
    # duas delas o planejador do SQLite escolheu produto cartesiano sobre o
    # índice único de `edge_window` — 72 mil × 72 mil no dado real — e o comando
    # nunca terminou. Um conjunto em memória custa alguns megabytes e é imune à
    # escolha do planejador.
    dentro = None
    if apenas_de:
        dentro = {r["actor_id"] for r in conn.execute(
            "SELECT actor_id FROM actor_community WHERE window_start=? "
            "AND scope=? AND graph_version=?", (window, apenas_de, graph_version))}

    cursor = conn.execute(f"""
        SELECT e.src_actor_id AS si, e.dst_actor_id AS di,
               COALESCE(s.handle, s.platform_user_id) AS src,
               COALESCE(d.handle, d.platform_user_id) AS dst,
               e.kind, e.weight
        FROM edge_window e
        JOIN actor s ON s.actor_id = e.src_actor_id
        JOIN actor d ON d.actor_id = e.dst_actor_id
        WHERE {' AND '.join(filtros)}
        ORDER BY e.weight DESC
    """, valores)

    def linhas():
        for r in cursor:
            if dentro is not None and (r["si"] not in dentro or r["di"] not in dentro):
                continue
            yield [r["src"], r["dst"], r["kind"], r["weight"]]

    return _escreve(destino / "edges.csv", ["src", "dst", "kind", "weight"], linhas())


def export_runs(conn: sqlite3.Connection, window: str, destino: Path) -> int:
    """Procedência: qual coleta alimentou esta janela, e com que volume."""
    janela_sql = WINDOW_SQL.format(col="p.created_at")
    linhas = conn.execute(f"""
        SELECT r.run_id, r.source, r.kind, r.campaign_label, r.query,
               r.started_at, r.status, r.cost_usd, COUNT(p.post_id) AS posts,
               MIN(substr(p.created_at,1,10)) AS de,
               MAX(substr(p.created_at,1,10)) AS ate
        FROM collection_run r
        JOIN post p ON p.run_id = r.run_id AND {janela_sql} = ?
        GROUP BY r.run_id ORDER BY posts DESC
    """, (window,))
    return _escreve(destino / "runs.csv",
                    ["run_id", "source", "kind", "campaign_label", "query",
                     "started_at", "status", "cost_usd", "posts", "de", "ate"],
                    linhas)


def manifest(conn: sqlite3.Connection, window: str, scope: str,
             contagens: dict[str, int],
             graph_version: int = GRAPH_VERSION) -> dict[str, Any]:
    """O que qualquer um precisa saber antes de usar estes CSVs.

    Inclui as ressalvas conhecidas do recorte. Elas viajam com o dado de
    propósito: quem recebe o arquivo não leu esta conversa, e número sem
    ressalva vira slide.
    """
    dias = [r["dia"] for r in conn.execute(
        f"SELECT DISTINCT substr(p.created_at,1,10) AS dia FROM post p "
        f"WHERE {WINDOW_SQL.format(col='p.created_at')} = ? ORDER BY dia",
        (window,))]
    termos = [r["t"] for r in conn.execute(f"""
        SELECT DISTINCT cr.campaign_label AS t FROM collection_run cr
        JOIN post p ON p.run_id = cr.run_id
        WHERE {WINDOW_SQL.format(col='p.created_at')} = ? AND cr.campaign_label IS NOT NULL
        ORDER BY t""", (window,))]

    ressalvas = [
        "E-I bruto (ei_mean) NÃO é comparável entre comunidades de tamanhos "
        "diferentes; use ei_choice, que só considera atores com mais de uma "
        "aresta. choice_actors diz quantos sustentam a média.",
        "PageRank é herdado: quem é amplificado por um hub recebe quase todo o "
        "rank dele. Leia sempre junto de in_degree_w.",
        "Comunidade é numerada por tamanho DENTRO desta janela. #0 é a maior "
        "aqui, e não corresponde à #0 de outra janela.",
    ]
    if scope.endswith(":core"):
        ressalvas.append(
            "Escopo de núcleo: só atores com mais de uma aresta, podados até o "
            "ponto fixo. Mede fechamento, não alcance — o alcance está no "
            "escopo cheio.")
    if len(dias) < 5:
        ressalvas.append(
            f"A janela tem só {len(dias)} dia(s) de coleta. Volume aqui reflete "
            f"tanto o mundo quanto a intensidade da coleta.")

    return {
        "gerado_em": utcnow(),
        "window_start": window,
        "scope": scope,
        "graph_version": graph_version,
        "dias_de_coleta": dias,
        "termos": termos,
        "contagens": contagens,
        "ressalvas": ressalvas,
    }
