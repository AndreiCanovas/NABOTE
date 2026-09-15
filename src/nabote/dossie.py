"""Os números do Dossiê: aprofundamento de UMA pauta, com escopo dedicado.

O radar decide onde aprofundar; o dossiê aprofunda. A diferença prática é o
escopo: tudo aqui é calculado dentro de `<view>:topic:<pauta>`, um grafo só com
as interações daquela coleta. O mesmo perfil pode ser central aqui e invisível
no grafo da semana — é esse contraste que justifica o recorte existir.

Quatro coisas que este módulo faz e que não existiam:

  mapa           subgrafo dos N mais centrais, com layout determinístico. O grafo
                 inteiro de uma pauta tem dezenas de milhares de nós e desenhá-lo
                 produz uma mancha; o recorte por centralidade é o que torna a
                 figura legível sem mentir, desde que o quanto ficou de fora
                 apareça no relatório.
  pontes         quem liga comunidades, medido de duas formas independentes:
                 peso que atravessa, e ponto de articulação. As duas concordando
                 é um sinal forte; só a primeira é fraco.
  coamplificação pares que amplificaram o mesmo alvo dentro de segundos.
  sub-pautas     dentro do tema, que enquadramento cada comunidade usa.

A escolha metodológica mais consequente está em `coamplification`: viralidade
em massa é excluída de propósito. Ver a docstring de lá.
"""

from __future__ import annotations

import math
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from .graph import (GRAPH_VERSION, LEIDEN_SEED, VIEWS, TOPIC_PREFIX,
                    edge_scope_of, _rng)

# Nós no mapa. Acima disso a figura vira mancha; abaixo, some a estrutura.
MAPA_TOP = 60
# Coamplificação: janela e o corte de viralidade (ver docstring).
COAMP_SEGUNDOS = 60
COAMP_GRUPO_MAX = 50
COAMP_MIN_PARES = 3
# Sub-pautas
NGRAM_MAX = 3
NGRAM_MIN_POSTS = 20
# Em quantos TEXTOS DIFERENTES o termo precisa aparecer. Sem isto, um post que
# viralizou basta para criar um "enquadramento".
NGRAM_MIN_TEXTOS = 5
NGRAM_MIN_LIFT = 1.5
SUBPAUTAS_POR_COMUNIDADE = 3
CONCENTRACAO_TOP = 3


def _fim(window_start: str) -> str:
    return (datetime.fromisoformat(window_start) + timedelta(days=7)).date().isoformat()


def topic_scope(view: str, label: str, core: bool = True) -> str:
    """Escopo analítico de uma pauta. Um lugar só monta este nome."""
    return f"{view}:{TOPIC_PREFIX}{label}" + (":core" if core else "")


# =============================================================================
# 1. RESUMO — os quatro tiles
# =============================================================================

def window_summary(conn: sqlite3.Connection, window_start: str, scope: str,
                   graph_version: int = GRAPH_VERSION) -> dict[str, Any]:
    """Atores, arestas por tipo, posts e custo da janela no escopo."""
    arestas = edge_scope_of(scope)
    por_kind = {r["kind"]: r["w"] for r in conn.execute(
        "SELECT kind, SUM(weight) AS w FROM edge_window "
        "WHERE window_start = ? AND scope = ? GROUP BY kind", (window_start, arestas))}
    total = sum(por_kind.values()) or 1.0

    atores = [r["actor_id"] for r in conn.execute(
        "SELECT actor_id FROM actor_community WHERE window_start = ? AND scope = ? "
        "AND graph_version = ?", (window_start, scope, graph_version))]
    tiers = Counter()
    if atores:
        marcas = ",".join("?" * len(atores))
        tiers = Counter(r["tier"] for r in conn.execute(
            f"SELECT tier FROM actor WHERE actor_id IN ({marcas})", atores))

    label = arestas[len(TOPIC_PREFIX):] if arestas.startswith(TOPIC_PREFIX) else None
    if label:
        onde, valores = "AND cr.campaign_label = ?", (window_start, _fim(window_start), label)
    else:
        onde, valores = "", (window_start, _fim(window_start))
    posts = conn.execute(
        f"SELECT COUNT(*) AS n, COUNT(DISTINCT p.actor_id) AS autores, "
        f"SUM(cr.cost_usd) AS custo, COUNT(DISTINCT p.run_id) AS runs "
        f"FROM post p JOIN collection_run cr ON cr.run_id = p.run_id "
        f"WHERE p.created_at >= ? AND p.created_at < ? {onde}", valores).fetchone()

    # custo é por run, não por post: somar cost_usd na junção multiplicaria
    # o custo de um run pelo número de posts dele
    custo = conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0) AS c FROM collection_run WHERE run_id IN "
        f"(SELECT DISTINCT p.run_id FROM post p JOIN collection_run cr ON cr.run_id = p.run_id "
        f" WHERE p.created_at >= ? AND p.created_at < ? {onde})", valores).fetchone()["c"]

    return {
        "atores": len(atores),
        "tier_a": tiers.get("A", 0), "tier_b": tiers.get("B", 0), "tier_c": tiers.get("C", 0),
        "arestas_peso": total,
        "mistura": {k: v / total for k, v in por_kind.items()},
        "posts": posts["n"], "autores": posts["autores"], "runs": posts["runs"],
        "custo_usd": custo,
    }


# =============================================================================
# 2. MAPA — subgrafo dos mais centrais, com layout determinístico
# =============================================================================

# Mínimo de audiência em comum para ligar dois perfis no mapa.
PROJECAO_MIN = 2


def network_map(conn: sqlite3.Connection, window_start: str, scope: str,
                limit: int = MAPA_TOP, view: str = "amp",
                graph_version: int = GRAPH_VERSION,
                projecao_min: int = PROJECAO_MIN) -> dict[str, Any]:
    """Subgrafo dos `limit` atores de maior PageRank, posicionado para desenho.

    A ARESTA AQUI NÃO É "A AMPLIFICOU B" — é "A e B têm audiência em comum",
    e a troca não é estética. Numa rede de amplificação quem tem PageRank alto
    é o amplificado, e amplificado não retuíta amplificado: no dado real, entre
    os 60 perfis mais centrais da pauta, o peso que atravessa comunidade somava
    4 de 169.234. Desenhar as arestas diretas produz um campo de estrelas sem
    linha nenhuma — uma figura que parece uma rede e não mostra rede alguma.

    A projeção por audiência compartilhada mostra o que a pergunta pede: dois
    perfis ficam ligados quando as mesmas pessoas amplificam os dois, que é
    exatamente o que torna um deles ponte entre comunidades.

    `arestas_diretas` volta junto porque o contraste entre os dois números é,
    ele próprio, um achado a registrar no relatório.
    """
    import igraph

    topo = conn.execute(
        "SELECT actor_id, value FROM actor_metric WHERE window_start = ? AND scope = ? "
        "AND metric = 'pagerank' AND graph_version = ? ORDER BY value DESC LIMIT ?",
        (window_start, scope, graph_version, limit)).fetchall()
    if not topo:
        return {"nos": [], "arestas": [], "de": 0, "cobertura_peso": 0.0,
                "arestas_diretas": 0, "peso_direto": 0.0, "ligacao": "audiencia"}
    ids = [r["actor_id"] for r in topo]
    pagerank = {r["actor_id"]: r["value"] for r in topo}
    indice = {a: i for i, a in enumerate(ids)}

    pesos = VIEWS[view]
    marcas_k = ",".join("?" * len(pesos))
    marcas_a = ",".join("?" * len(ids))
    diretas: dict[tuple[int, int], float] = {}
    total_peso = 0.0
    # audiência de cada perfil selecionado, para a projeção
    audiencia: dict[int, set[int]] = {a: set() for a in ids}
    for r in conn.execute(
        f"SELECT src_actor_id s, dst_actor_id d, kind, weight FROM edge_window "
        f"WHERE window_start = ? AND scope = ? AND kind IN ({marcas_k})",
        (window_start, edge_scope_of(scope), *pesos.keys())
    ):
        w = r["weight"] * pesos[r["kind"]]
        total_peso += w
        if r["d"] in audiencia:
            audiencia[r["d"]].add(r["s"])
        if r["s"] in indice and r["d"] in indice:
            chave = (indice[r["s"]], indice[r["d"]])
            diretas[chave] = diretas.get(chave, 0.0) + w

    comum: dict[tuple[int, int], int] = {}
    for x in range(len(ids)):
        ax = audiencia[ids[x]]
        if not ax:
            continue
        for y in range(x + 1, len(ids)):
            n = len(ax & audiencia[ids[y]])
            if n >= projecao_min:
                comum[(x, y)] = n

    g = igraph.Graph(directed=False)
    g.add_vertices(len(ids))
    if comum:
        g.add_edges(list(comum))
        g.es["weight"] = [float(v) for v in comum.values()]
    # Fruchterman-Reingold é estocástico: sem semente a mesma janela sai com o
    # mapa embaralhado a cada execução e ninguém consegue comparar duas.
    with _rng(LEIDEN_SEED):
        pos = g.layout_fruchterman_reingold(weights="weight" if comum else None)
    xs = [p[0] for p in pos] or [0.0]
    ys = [p[1] for p in pos] or [0.0]
    dx = (max(xs) - min(xs)) or 1.0
    dy = (max(ys) - min(ys)) or 1.0

    meta = {r["actor_id"]: r for r in conn.execute(
        f"SELECT actor_id, handle, tier FROM actor WHERE actor_id IN ({marcas_a})", ids)}
    com = {r["actor_id"]: r["community_id"] for r in conn.execute(
        f"SELECT actor_id, community_id FROM actor_community WHERE window_start = ? "
        f"AND scope = ? AND graph_version = ? AND actor_id IN ({marcas_a})",
        (window_start, scope, graph_version, *ids))}
    ei = {r["actor_id"]: r["value"] for r in conn.execute(
        f"SELECT actor_id, value FROM actor_metric WHERE window_start = ? AND scope = ? "
        f"AND metric = 'ei_index' AND graph_version = ? AND actor_id IN ({marcas_a})",
        (window_start, scope, graph_version, *ids))}

    nos = [{
        "actor_id": a, "handle": (meta[a]["handle"] if a in meta else None),
        "tier": (meta[a]["tier"] if a in meta else "C"),
        "comunidade": com.get(a), "pagerank": pagerank[a], "ei": ei.get(a),
        "audiencia": len(audiencia[a]),
        "x": (pos[i][0] - min(xs)) / dx, "y": (pos[i][1] - min(ys)) / dy,
    } for i, a in enumerate(ids)]

    return {
        "nos": nos,
        "arestas": [{"de": x, "para": y, "peso": float(n)}
                    for (x, y), n in sorted(comum.items())],
        "ligacao": "audiencia",
        "arestas_diretas": len(diretas),
        "peso_direto": sum(diretas.values()),
        "de": conn.execute(
            "SELECT COUNT(*) AS n FROM actor_community WHERE window_start = ? AND "
            "scope = ? AND graph_version = ?", (window_start, scope, graph_version)
        ).fetchone()["n"],
        "cobertura_peso": (sum(diretas.values()) / total_peso) if total_peso else 0.0,
    }


def bridges(mapa: dict[str, Any], top: int = 3) -> list[dict[str, Any]]:
    """Quem liga comunidades, por duas medidas independentes.

    `peso_externo` é quanto do peso do ator cruza fronteira. `articulacao` diz
    se remover o ator parte o grafo em dois — é a afirmação forte, e ela vem do
    igraph, não de um limiar escolhido à mão. As duas juntas é o que separa
    "fala com os dois lados" de "é o caminho entre os dois lados".
    """
    import igraph

    nos = {n["actor_id"]: n for n in mapa["nos"]}
    if not nos:
        return []
    ids = list(nos)
    indice = {a: i for i, a in enumerate(ids)}

    # "peso" aqui é audiência em comum, não volume amplificado — ver network_map
    externo: dict[int, float] = {a: 0.0 for a in ids}
    total: dict[int, float] = {a: 0.0 for a in ids}
    g = igraph.Graph()
    g.add_vertices(len(ids))
    pares = set()
    for e in mapa["arestas"]:
        s, d, w = ids[e["de"]], ids[e["para"]], e["peso"]
        total[s] += w
        total[d] += w
        if nos[s]["comunidade"] != nos[d]["comunidade"]:
            externo[s] += w
            externo[d] += w
        par = (min(indice[s], indice[d]), max(indice[s], indice[d]))
        if par[0] != par[1]:
            pares.add(par)
    g.add_edges(sorted(pares))
    corta = {ids[i] for i in g.articulation_points()} if g.ecount() else set()

    linhas = [{
        "actor_id": a, "handle": nos[a]["handle"], "comunidade": nos[a]["comunidade"],
        "peso_externo": externo[a],
        "fatia_externa": (externo[a] / total[a]) if total[a] else 0.0,
        "articulacao": a in corta, "ei": nos[a]["ei"],
    } for a in ids if externo[a] > 0]
    linhas.sort(key=lambda r: (not r["articulacao"], -r["peso_externo"]))
    return linhas[:top]


# =============================================================================
# 3. COAMPLIFICAÇÃO
# =============================================================================

def coamplification(conn: sqlite3.Connection, window_start: str, scope: str,
                    segundos: int = COAMP_SEGUNDOS,
                    grupo_max: int = COAMP_GRUPO_MAX,
                    min_pares: int = COAMP_MIN_PARES) -> dict[str, Any]:
    """Pares de contas que amplificaram o mesmo alvo dentro de `segundos`.

    Duas limitações declaradas, porque as duas mudam a leitura:

    1. O ALVO É O ATOR, NÃO O POST. Esta base não traz o id do post original
       retuitado — só quem foi retuitado. Então "amplificaram o mesmo post"
       vira "amplificaram a mesma conta no mesmo instante", que é mais frouxo:
       duas pessoas retuitando coisas diferentes do mesmo perfil ao mesmo tempo
       contam aqui. Com coleta pela API o id vem junto e o critério aperta sem
       mudar o resto.

    2. VIRALIDADE EM MASSA FICA DE FORA. Um post que 5.000 contas amplificam no
       mesmo minuto gera 12 milhões de pares simultâneos e nenhuma informação:
       quando todo mundo está junto, estar junto não distingue ninguém. Grupos
       acima de `grupo_max` são excluídos e a contagem deles volta em
       `grupos_ignorados` — o resíduo é declarado, não escondido.
    """
    fim = _fim(window_start)
    arestas = edge_scope_of(scope)
    if arestas.startswith(TOPIC_PREFIX):
        recorte = ("JOIN post p ON p.post_id = i.post_id "
                   "JOIN collection_run cr ON cr.run_id = p.run_id", "AND cr.campaign_label = ?")
        valores = (window_start, fim, arestas[len(TOPIC_PREFIX):])
    else:
        recorte, valores = ("", ""), (window_start, fim)

    grupos: dict[int, list[tuple[int, int]]] = {}
    for r in conn.execute(
        f"SELECT i.dst_actor_id d, i.src_actor_id s, i.occurred_at t FROM interaction i "
        f"{recorte[0]} WHERE i.kind = 'repost' AND i.occurred_at >= ? AND i.occurred_at < ? "
        f"{recorte[1]} ORDER BY i.dst_actor_id, i.occurred_at", valores
    ):
        quando = int(datetime.fromisoformat(r["t"].replace("Z", "+00:00")).timestamp())
        grupos.setdefault(r["d"], []).append((quando, r["s"]))

    pares: dict[tuple[int, int], list[int]] = {}
    ignorados = 0
    for eventos in grupos.values():
        if len(eventos) > grupo_max:
            ignorados += 1
            continue
        for i, (t1, a1) in enumerate(eventos):
            for t2, a2 in eventos[i + 1:]:
                if t2 - t1 > segundos:
                    break
                if a1 == a2:
                    continue
                pares.setdefault((min(a1, a2), max(a1, a2)), []).append(t2 - t1)

    fortes = {p: v for p, v in pares.items() if len(v) >= min_pares}
    # clusters = componentes conexos do grafo de pares
    vizinhos: dict[int, set[int]] = {}
    for a, b in fortes:
        vizinhos.setdefault(a, set()).add(b)
        vizinhos.setdefault(b, set()).add(a)
    vistos: set[int] = set()
    clusters: list[dict[str, Any]] = []
    for semente in sorted(vizinhos):
        if semente in vistos:
            continue
        pilha, grupo = [semente], set()
        while pilha:
            atual = pilha.pop()
            if atual in grupo:
                continue
            grupo.add(atual)
            pilha.extend(vizinhos[atual] - grupo)
        vistos |= grupo
        intervalos = sorted(x for (a, b), v in fortes.items()
                            if a in grupo and b in grupo for x in v)
        clusters.append({
            "contas": sorted(grupo), "n_contas": len(grupo),
            "coamplificacoes": len(intervalos),
            "mediana_s": intervalos[len(intervalos) // 2] if intervalos else None,
        })
    clusters.sort(key=lambda c: -c["coamplificacoes"])
    return {
        "clusters": clusters, "grupos": len(grupos), "grupos_ignorados": ignorados,
        "pares_testados": len(pares), "chave": "ator-alvo",
        "segundos": segundos, "grupo_max": grupo_max,
    }


# =============================================================================
# 4. SUB-PAUTAS — enquadramentos dentro do tema
# =============================================================================

_STOP = set("""
a ao aos as à às com como da das de dela dele deles do dos e em entre era eram essa
esse esta este eu foi for foram há isso isto já lhe mais mas me mesmo meu muito na
nas nem no nos não nós o os ou para pela pelas pelo pelos por porque qual quando que
quem se sem ser seu seusso sobre sua suas são só também te tem tinha tu um uma umas
uns vai você vocês eles elas nossa nosso pra pro aqui ali lá agora hoje ontem sim
ainda até assim cada depois desde essas esses estas estes outra outro outras outros
pois quer tao tão toda todas todo todos vez vezes ver vai vao vão fazer faz feito
ter tem ter sao ha rt https http co t www com br
veja assista olha vejam confira acompanhe leia saiba clique link fio thread
contra sobre apos antes durante onde quem qual quais porque pois logo entao
situacao caso coisa coisas parte partes forma jeito modo tipo tipos lugar
dia dias mes meses ano anos hora horas tempo momento vez momento gente pessoas
pessoa povo brasil brasileiro brasileira brasileiros brasileiras pais nacional
governo presidente ministro ministra ministerio federal publico publica
grande grandes pequeno pequena novo nova velho bom boa mal melhor pior
""".split())


def _tokens(texto: str) -> list[str]:
    """Minúsculas, sem acento, sem URL, sem @ e sem #."""
    t = texto.lower()
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)
    t = re.sub(r"[@#]\w+", " ", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w for w in re.findall(r"[a-z]{3,}", t) if w not in _STOP]


def _passar(conn: sqlite3.Connection, window_start: str, label: str | None):
    """Lê os posts da pauta uma vez e devolve (ator, tokens colados).

    Colar os tokens num string só em vez de guardar a lista é o que mantém o
    corpus inteiro na memória: 143 mil posts viram ~20 MB de texto em vez de
    três milhões de objetos Python.
    """
    if label:
        onde, valores = "AND cr.campaign_label = ?", (window_start, _fim(window_start), label)
    else:
        onde, valores = "", (window_start, _fim(window_start))
    total = 0
    docs: list[tuple[int, str]] = []
    for r in conn.execute(
        f"SELECT p.actor_id AS a, p.text AS texto FROM post p "
        f"JOIN collection_run cr ON cr.run_id = p.run_id "
        f"WHERE p.created_at >= ? AND p.created_at < ? {onde}", valores
    ):
        total += 1
        if r["texto"]:
            docs.append((r["a"], " ".join(_tokens(r["texto"]))))
    return total, docs


def _frequentes(docs, comunidade, n: int, anteriores: set[str] | None,
                min_posts: int, min_textos: int):
    """Frequência dos n-gramas: por post (para o share) e por TEXTO DISTINTO.

    Só monta n-gramas cujos dois pedaços de tamanho n-1 já passaram no corte.
    Sem essa poda, trigramas de um corpus de 143 mil posts geram milhões de
    chaves distintas e o processo morre por memória antes de responder nada —
    é a mesma ideia do Apriori, e é o que torna esta seção viável sem numpy.

    A contagem por texto distinto existe por causa de um problema que só
    aparece em corpus de retuíte: um post que viraliza 87 vezes entra como 87
    documentos IDÊNTICOS, e cada n-grama daquele texto herda frequência 87. Na
    primeira versão isso produziu, numa comunidade, três "enquadramentos"
    com posts=87 e lift=89,4 iguais — eram três fragmentos da MESMA frase.
    A seleção passa a exigir que o termo apareça em vários textos diferentes;
    o share continua sendo volume, que é o que o relatório pede.
    """
    doc_freq: Counter = Counter()
    textos_freq: Counter = Counter()
    por_com: dict[int, Counter] = {}
    vistos_por_texto: dict[str, set[str]] = {}
    for actor_id, texto in docs:
        if texto in vistos_por_texto:
            vistos = vistos_por_texto[texto]
        else:
            palavras = texto.split()
            vistos = set()
            for i in range(len(palavras) - n + 1):
                if anteriores is not None:
                    if " ".join(palavras[i:i + n - 1]) not in anteriores:
                        continue
                    if " ".join(palavras[i + 1:i + n]) not in anteriores:
                        continue
                vistos.add(" ".join(palavras[i:i + n]))
            vistos_por_texto[texto] = vistos
            textos_freq.update(vistos)
        if not vistos:
            continue
        doc_freq.update(vistos)
        cid = comunidade.get(actor_id)
        if cid is not None:
            por_com.setdefault(cid, Counter()).update(vistos)
    sobrevivem = {g for g in doc_freq
                  if doc_freq[g] >= min_posts and textos_freq[g] >= min_textos}
    return doc_freq, por_com, sobrevivem, textos_freq


def subtopics(conn: sqlite3.Connection, window_start: str, scope: str,
              por_comunidade: int = SUBPAUTAS_POR_COMUNIDADE,
              min_posts: int = NGRAM_MIN_POSTS, min_lift: float = NGRAM_MIN_LIFT,
              min_textos: int = NGRAM_MIN_TEXTOS,
              graph_version: int = GRAPH_VERSION) -> dict[str, Any]:
    """Enquadramentos distintivos de cada comunidade, dentro da pauta.

    O nível 1 do plano, na versão que não precisa de modelo nem de rede: o
    enquadramento é o n-grama que a comunidade usa DESPROPORCIONALMENTE. `lift`
    é P(termo|comunidade) / P(termo) e é o que separa "eles falam disso" de
    "só eles falam disso"; `share` sozinho devolveria as palavras mais comuns
    do português.

    Ao contrário do share por ator — que exige o total publicado por cada conta
    e por isso não se sustenta em coleta por termo — este share tem denominador:
    é a fatia dos posts DESTE corpus, e o corpus está inteiro no banco.
    """
    arestas = edge_scope_of(scope)
    label = arestas[len(TOPIC_PREFIX):] if arestas.startswith(TOPIC_PREFIX) else None

    comunidade = {r["actor_id"]: r["community_id"] for r in conn.execute(
        "SELECT actor_id, community_id FROM actor_community WHERE window_start = ? "
        "AND scope = ? AND graph_version = ?", (window_start, scope, graph_version))}

    total_posts, docs = _passar(conn, window_start, label)
    com_texto = len(docs)
    # O próprio termo da coleta está em todo post por construção: mantê-lo
    # devolveria "Yanomami" como o enquadramento distintivo de toda comunidade.
    proibidos = set(_tokens(label)) if label else set()
    if proibidos:
        docs = [(a, " ".join(w for w in t.split() if w not in proibidos))
                for a, t in docs]

    freq: dict[str, int] = {}
    textos: dict[str, int] = {}
    contagem: dict[int, Counter] = {}
    posts_com: Counter = Counter()
    for actor_id, _ in docs:
        cid = comunidade.get(actor_id)
        if cid is not None:
            posts_com[cid] += 1

    sobrevivem: set[str] | None = None
    for n in range(1, NGRAM_MAX + 1):
        doc_freq, por_com, sobrevivem_n, textos_n = _frequentes(
            docs, comunidade, n, sobrevivem, min_posts, min_textos)
        if not sobrevivem_n:
            break
        freq.update({g: doc_freq[g] for g in sobrevivem_n})
        textos.update({g: textos_n[g] for g in sobrevivem_n})
        for cid, c in por_com.items():
            alvo = contagem.setdefault(cid, Counter())
            for g in sobrevivem_n:
                if c[g]:
                    alvo[g] = c[g]
        sobrevivem = sobrevivem_n

    escolhidos: dict[int, list[tuple]] = {}
    for cid, c in contagem.items():
        n_com = posts_com[cid] or 1
        candidatos = []
        for termo, n in c.items():
            if n < min_posts:
                continue
            share = n / n_com
            base = freq[termo] / com_texto if com_texto else 0.0
            lift = share / base if base else 0.0
            if lift < min_lift:
                continue
            candidatos.append((lift, share, termo, n))
        candidatos.sort(key=lambda x: (-x[0] * x[1], x[2]))
        ficam: list[tuple] = []
        for cand in candidatos:
            # Rejeita por PALAVRA em comum, não por substring. "estamos falando
            # genocidio" e "falando genocidio marreco" são janelas deslizantes
            # da mesma frase e nenhuma é substring da outra: a regra antiga
            # deixava as duas passarem e a tabela saía com três linhas
            # descrevendo um enquadramento só.
            palavras = set(cand[2].split())
            if any(palavras & set(j[2].split()) for j in ficam):
                continue
            ficam.append(cand)
            if len(ficam) >= por_comunidade:
                break
        if ficam:
            escolhidos[cid] = ficam

    # Concentração só dos termos que ficaram: uma passada barata sobre poucos
    # termos, em vez de guardar um Counter de autores por n-grama do corpus.
    alvos = {(cid, t[2]) for cid, linhas in escolhidos.items() for t in linhas}
    autores: dict[tuple[int, str], Counter] = {k: Counter() for k in alvos}
    if alvos:
        por_termo: dict[str, set[int]] = {}
        for cid, termo in alvos:
            por_termo.setdefault(termo, set()).add(cid)
        for actor_id, texto in docs:
            cid = comunidade.get(actor_id)
            if cid is None:
                continue
            for termo, cids in por_termo.items():
                if cid in cids and termo in texto:
                    autores[(cid, termo)][actor_id] += 1

    linhas: list[dict[str, Any]] = []
    for cid, itens in escolhidos.items():
        for lift, share, termo, n in itens:
            topo = autores[(cid, termo)].most_common(CONCENTRACAO_TOP)
            linhas.append({
                "comunidade": cid, "termo": termo, "posts": n,
                "textos": textos.get(termo, 0),
                "share": share, "lift": lift,
                "concentracao": sum(c for _, c in topo) / n if n else 0.0,
                "top_autores": [a for a, _ in topo],
            })
    linhas.sort(key=lambda r: (r["comunidade"], -r["lift"]))
    return {
        "linhas": linhas, "posts": total_posts, "posts_com_texto": com_texto,
        "textos_distintos": len({t for _, t in docs}),
        "metodo": (f"ngram-v2(n<={NGRAM_MAX},posts>={min_posts},"
                   f"textos>={min_textos},lift>={min_lift})"),
    }


# =============================================================================
# 5. ATORES E O SNAPSHOT COMPLETO
# =============================================================================

def top_actors(conn: sqlite3.Connection, window_start: str, scope: str,
               limit: int = 12, graph_version: int = GRAPH_VERSION,
               janela_anterior: str | None = None) -> list[dict[str, Any]]:
    """Top por PageRank no escopo da pauta, com eixo, E-I, tier e Δ posição."""
    from .positions import positions_of

    linhas = conn.execute(
        "SELECT actor_id, value FROM actor_metric WHERE window_start = ? AND scope = ? "
        "AND metric = 'pagerank' AND graph_version = ? ORDER BY value DESC LIMIT ?",
        (window_start, scope, graph_version, limit)).fetchall()
    if not linhas:
        return []
    ids = [r["actor_id"] for r in linhas]
    marcas = ",".join("?" * len(ids))

    def _metrica(nome: str) -> dict[int, float]:
        return {r["actor_id"]: r["value"] for r in conn.execute(
            f"SELECT actor_id, value FROM actor_metric WHERE window_start = ? AND "
            f"scope = ? AND metric = ? AND graph_version = ? AND actor_id IN ({marcas})",
            (window_start, scope, nome, graph_version, *ids))}

    indeg = _metrica("in_degree_w")
    ei = _metrica("ei_index")
    com = {r["actor_id"]: r["community_id"] for r in conn.execute(
        f"SELECT actor_id, community_id FROM actor_community WHERE window_start = ? "
        f"AND scope = ? AND graph_version = ? AND actor_id IN ({marcas})",
        (window_start, scope, graph_version, *ids))}
    meta = {r["actor_id"]: r for r in conn.execute(
        f"SELECT actor_id, handle, tier, is_public_figure FROM actor "
        f"WHERE actor_id IN ({marcas})", ids)}
    eixo = positions_of(conn, window_start, scope, ids)
    antes = positions_of(conn, janela_anterior, scope) if janela_anterior else {}

    saida = []
    for r in linhas:
        a = r["actor_id"]
        saida.append({
            "actor_id": a, "handle": meta[a]["handle"] if a in meta else None,
            "tier": meta[a]["tier"] if a in meta else "C",
            "figura_publica": bool(meta[a]["is_public_figure"]) if a in meta else False,
            "comunidade": com.get(a), "pagerank": r["value"],
            "in_degree": indeg.get(a), "ei": ei.get(a),
            "eixo": eixo.get(a),
            "delta_eixo": (eixo[a] - antes[a]) if a in eixo and a in antes else None,
        })
    return saida


def community_rows(conn: sqlite3.Connection, window_start: str, scope: str,
                   subpautas: dict[str, Any] | None = None,
                   graph_version: int = GRAPH_VERSION) -> list[dict[str, Any]]:
    """Cartões de comunidade dentro da pauta: tamanho, E-I, eixo médio, termos."""
    from .positions import positions_of

    eixo = positions_of(conn, window_start, scope)
    membro = {r["actor_id"]: r["community_id"] for r in conn.execute(
        "SELECT actor_id, community_id FROM actor_community WHERE window_start = ? "
        "AND scope = ? AND graph_version = ?", (window_start, scope, graph_version))}
    por_com: dict[int, list[float]] = {}
    for a, s in eixo.items():
        if a in membro:
            por_com.setdefault(membro[a], []).append(s)

    termos: dict[int, list[dict[str, Any]]] = {}
    for linha in (subpautas or {}).get("linhas", []):
        termos.setdefault(linha["comunidade"], []).append(linha)

    saida = []
    for r in conn.execute(
        "SELECT community_id, size, ei_mean, ei_choice, choice_actors FROM community "
        "WHERE window_start = ? AND scope = ? AND graph_version = ? ORDER BY size DESC",
        (window_start, scope, graph_version)
    ):
        cid = r["community_id"]
        medias = por_com.get(cid, [])
        saida.append({
            "id": cid, "atores": r["size"], "ei": r["ei_choice"], "ei_bruto": r["ei_mean"],
            "n_escolha": r["choice_actors"],
            "eixo": (sum(medias) / len(medias)) if medias else None,
            "n_eixo": len(medias),
            "termos": termos.get(cid, []),
        })
    return saida


def snapshot(conn: sqlite3.Connection, window_start: str, label: str,
             view: str = "amp", core: bool = True, top: int = 12,
             mapa_top: int = MAPA_TOP,
             janela_anterior: str | None = None) -> dict[str, Any]:
    """Tudo que o Dossiê precisa de uma pauta numa janela, num objeto só."""
    scope = topic_scope(view, label, core)
    sub = subtopics(conn, window_start, scope)
    mapa = network_map(conn, window_start, scope, limit=mapa_top, view=view)
    return {
        "janela": window_start, "pauta": label, "escopo": scope, "view": view,
        "resumo": window_summary(conn, window_start, scope),
        "mapa": mapa,
        "pontes": bridges(mapa),
        "atores": top_actors(conn, window_start, scope, limit=top,
                             janela_anterior=janela_anterior),
        "comunidades": community_rows(conn, window_start, scope, sub),
        "subpautas": sub,
        "coamplificacao": coamplification(conn, window_start, scope),
    }
