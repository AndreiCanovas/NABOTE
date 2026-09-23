"""Tema descoberto no texto, confirmado por curadoria.

A pauta não vem mais de um rótulo de coleta. Ela sai do que foi escrito, por um
caminho de quatro passos: termo -> coocorrência -> grupo -> confirmação.

POR QUE COOCORRÊNCIA E NÃO EMBEDDING. Dois termos que aparecem no mesmo post
estão ligados; a rede dessas ligações se parte em grupos pelo mesmo algoritmo de
comunidade que já parte a rede de atores. Isso reaproveita o igraph que já é
dependência, roda em milissegundos, e o resultado é inspecionável: dá para
apontar quais posts ligaram "cpmi" a "inss". Embedding acertaria mais em
sinônimo e ironia, ao custo de uma dependência pesada e de um resultado que
ninguém consegue auditar — troca ruim para um instrumento cujo compromisso é
que todo número seja verificável.

POR QUE A CONFIRMAÇÃO HUMANA É ESTRUTURAL. Agrupamento não devolve os mesmos
grupos em duas rodadas. Sem ela, comparar semanas compararia coisas diferentes
com o mesmo nome. Confirmar congela: o tema ganha id, nome e um conjunto de
termos que passa a casar os posts das janelas seguintes. O dicionário existe e
ninguém o escreve — ele é o rastro das confirmações.

E O RESÍDUO É O PRODUTO. O que não casou com nenhum tema já decidido é o
material da proposta seguinte. É ali que pauta nova aparece, que era exatamente
o buraco de um léxico escrito à mão: assunto novo não existe até alguém digitar.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections import Counter
from itertools import combinations
from typing import Any, Iterable

from .db import utcnow
from .graph import WINDOW_SQL

METODO = "cooc"
VERSAO = 1

# Termo que aparece uma vez só não tem com o que coocorrer, e viraria um "tema"
# de um post.
FREQ_MINIMA = 2
# Quantos termos de um tema um post precisa ter para ser daquele tema. Um só
# casaria "governo" com metade da política brasileira.
TERMOS_PARA_CASAR = 2
# Grupo menor que isto é respingo, não pauta.
POSTS_MINIMOS = 3
# Quantos termos entram no rótulo proposto.
TERMOS_NO_ROTULO = 3

# Palavras que carregam estrutura da frase e não assunto. Lista curta de
# propósito: cortar demais apaga pauta ("governo", "lei" e "voto" ficam).
VAZIAS = set("""
a o e de da do das dos em no na nos nas para por com sem sobre que quem se sua
seu suas seus um uma uns umas as os ao aos pelo pela pelos pelas ja mais nao
tem ter foi vai vao ser sera sendo ou como mas todo toda todos todas tudo la
ele ela eles elas isso isto este esta esse essa aquele aquela aqui ali entao
ate contra desde apos antes agora hoje ontem amanha muito pouco bem mais menos
so tambem ainda porque pois quando onde qual quais quanto quantos cada outro
outra outros outras mesmo mesma proprio propria fazer feito faz diz disse dizer
pode podem deve devem quer querem vamos vou estou esta estao era eram sao
""".split())


def normalizar(texto: str) -> str:
    """Minúscula sem acento: 'Tarifaço' e 'tarifaco' são a mesma palavra, e
    tratá-las como duas racha o tema exatamente onde ele é mais forte."""
    decomposto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in decomposto if unicodedata.category(c) != "Mn")


def extrair_termos(texto: str | None) -> set[str]:
    """Os termos com conteúdo de um post.

    Hashtag entra inteira porque hashtag É pauta — é o rótulo que a própria
    rede deu ao assunto. Número entra só com quatro dígitos: '2026' é pauta,
    '50' é ruído de '50% de tarifa'.
    """
    if not texto:
        return set()
    puro = normalizar(texto)
    fora: set[str] = set()
    for p in re.findall(r"#?[a-z0-9_]+", puro):
        if p.startswith("#") and len(p) > 2:
            fora.add(p)
        elif p.isdigit():
            if len(p) == 4:
                fora.add(p)
        elif len(p) > 3 and p not in VAZIAS:
            fora.add(p)
    return fora


def agrupar_termos(termos_por_post: list[set[str]],
                   freq_minima: int = FREQ_MINIMA) -> list[list[str]]:
    """Grupos de termos que andam juntos, pela mesma comunidade do grafo."""
    import igraph as ig

    freq = Counter(t for s in termos_por_post for t in s)
    vocab = {t for t, n in freq.items() if n >= freq_minima}
    if len(vocab) < 2:
        return []

    pesos: Counter = Counter()
    for s in termos_por_post:
        for a, b in combinations(sorted(s & vocab), 2):
            pesos[(a, b)] += 1
    if not pesos:
        return []

    nomes = sorted(vocab)
    idx = {n: i for i, n in enumerate(nomes)}
    g = ig.Graph(len(nomes))
    g.vs["name"] = nomes
    g.add_edges([(idx[a], idx[b]) for a, b in pesos])
    g.es["weight"] = list(pesos.values())

    grupos = []
    for comunidade in g.community_multilevel(weights="weight"):
        ts = sorted((g.vs[v]["name"] for v in comunidade), key=lambda t: -freq[t])
        if len(ts) >= 2:
            grupos.append(ts)
    return grupos


def _posts_da_janela(conn: sqlite3.Connection, window_start: str,
                     platform: str | None) -> list[tuple[int, set[str]]]:
    sql = (f"SELECT post_id, text FROM post "
           f"WHERE {WINDOW_SQL.format(col='created_at')} = ? AND text IS NOT NULL")
    args: list[Any] = [window_start]
    if platform:
        sql += " AND platform = ?"
        args.append(platform)
    return [(r["post_id"], extrair_termos(r["text"]))
            for r in conn.execute(sql, args)]


def _temas_decididos(conn: sqlite3.Connection) -> list[dict]:
    """Confirmados E descartados. Os dois absorvem post: o confirmado porque é
    o tema dele, o descartado porque recusar uma vez tem que valer para as
    próximas janelas — senão a curadoria vira trabalho de Sísifo."""
    return [{"topic_id": r["topic_id"], "status": r["status"],
             "termos": set(json.loads(r["terms"] or "[]"))}
            for r in conn.execute(
                "SELECT topic_id, status, terms FROM topic "
                "WHERE status IN ('confirmado','descartado') "
                "AND method = ? AND version = ?", (METODO, VERSAO))]


def propor_temas(conn: sqlite3.Connection, window_start: str, *,
                 platform: str | None = None,
                 posts_minimos: int = POSTS_MINIMOS) -> list[dict]:
    """Processa uma janela: casa com o que já foi decidido, propõe o resto.

    Idempotente por janela: rodar duas vezes não duplica tema nem vínculo.
    """
    posts = _posts_da_janela(conn, window_start, platform)
    decididos = _temas_decididos(conn)

    # 1. o que casa com tema já decidido sai do material de proposta
    sobra: list[tuple[int, set[str]]] = []
    for post_id, termos in posts:
        casou = False
        for tema in decididos:
            comuns = termos & tema["termos"]
            if len(comuns) >= TERMOS_PARA_CASAR:
                casou = True
                if tema["status"] == "confirmado":
                    conn.execute(
                        "INSERT INTO post_topic (post_id, topic_id, score, "
                        "method_version) VALUES (?,?,?,?) "
                        "ON CONFLICT (post_id, topic_id, method_version) "
                        "DO UPDATE SET score = excluded.score",
                        (post_id, tema["topic_id"],
                         len(comuns) / max(len(tema["termos"]), 1), VERSAO))
        if not casou:
            sobra.append((post_id, termos))

    # 2. o resíduo vira proposta
    for grupo in agrupar_termos([t for _, t in sobra]):
        conjunto = set(grupo)
        dentro = [pid for pid, t in sobra
                  if len(t & conjunto) >= TERMOS_PARA_CASAR]
        if len(dentro) < posts_minimos:
            continue
        rotulo = " / ".join(grupo[:TERMOS_NO_ROTULO])
        ja = conn.execute(
            "SELECT topic_id FROM topic WHERE label = ? AND version = ?",
            (rotulo, VERSAO)).fetchone()
        if ja:
            topic_id = ja["topic_id"]
            conn.execute("UPDATE topic SET terms = ? WHERE topic_id = ?",
                         (json.dumps(grupo, ensure_ascii=False), topic_id))
        else:
            topic_id = conn.execute(
                "INSERT INTO topic (label, description, method, version, "
                "created_at, terms, status, window_start) "
                "VALUES (?,?,?,?,?,?, 'proposto', ?)",
                (rotulo, f"{len(dentro)} posts na janela {window_start}",
                 METODO, VERSAO, utcnow(),
                 json.dumps(grupo, ensure_ascii=False), window_start)).lastrowid
        for pid in dentro:
            conn.execute(
                "INSERT INTO post_topic (post_id, topic_id, score, "
                "method_version) VALUES (?,?,?,?) "
                "ON CONFLICT (post_id, topic_id, method_version) DO NOTHING",
                (pid, topic_id, len(conjunto) and 1.0, VERSAO))
    conn.commit()
    return [t for t in listar_temas(conn, status="proposto")
            if t["window_start"] == window_start]


def confirmar_tema(conn: sqlite3.Connection, topic_id: int, label: str) -> None:
    """Congela o tema: id estável, nome de gente, termos que casam as próximas
    janelas. É este passo que torna "CPMI subiu 40%" uma frase com sentido."""
    conn.execute("UPDATE topic SET label = ?, status = 'confirmado' "
                 "WHERE topic_id = ?", (label.strip(), topic_id))
    conn.commit()


def descartar_tema(conn: sqlite3.Connection, topic_id: int) -> None:
    """Recusa que vale para sempre. O tema fica no banco justamente para não
    voltar a ser proposto na janela seguinte."""
    conn.execute("UPDATE topic SET status = 'descartado' WHERE topic_id = ?",
                 (topic_id,))
    conn.commit()


def listar_temas(conn: sqlite3.Connection, status: str | None = None,
                 window_start: str | None = None) -> list[dict]:
    sql = ("SELECT t.topic_id, t.label, t.status, t.terms, t.window_start, "
           "(SELECT COUNT(*) FROM post_topic pt WHERE pt.topic_id = t.topic_id) posts "
           "FROM topic t WHERE t.method = ? AND t.version = ?")
    args: list[Any] = [METODO, VERSAO]
    if status:
        sql += " AND t.status = ?"
        args.append(status)
    if window_start:
        sql += " AND t.window_start = ?"
        args.append(window_start)
    return [{"topic_id": r["topic_id"], "label": r["label"], "status": r["status"],
             "termos": json.loads(r["terms"] or "[]"),
             "window_start": r["window_start"], "posts": r["posts"]}
            for r in conn.execute(sql + " ORDER BY posts DESC, t.topic_id", args)]
