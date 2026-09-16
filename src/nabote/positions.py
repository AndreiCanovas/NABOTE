"""Eixo de posicionamento: análise de correspondência sobre a matriz de amplificação.

A pergunta que isto responde é "quem está perto de quem", e a resposta vem da
ESTRUTURA, não do texto: dois atores ficam próximos quando amplificam as mesmas
contas, mesmo que nunca tenham escrito uma palavra parecida. É a mesma família
de método que a literatura chama de ideologia latente.

Três decisões que valem registro, porque cada uma é uma armadilha:

1. NÃO USAMOS numpy. O projeto tem duas dependências e nenhuma delas é numpy;
   acrescentar uma biblioteca inteira para extrair um vetor singular de uma
   matriz esparsa não se paga. A primeira dimensão sai por iteração de potência
   com deflação, que é O(arestas) por passo e cabe em 60 linhas.

2. QUEM AMPLIFICOU UMA CONTA SÓ NÃO TEM POSIÇÃO. Um ator com um único alvo cai
   exatamente em cima dele — a coordenada existe e não significa nada. É o mesmo
   raciocínio do E-I "com escolha": sem escolha não há posição. Por isso o filtro
   roda até o ponto fixo, como a poda do núcleo.

3. O SINAL DO EIXO É CONVENÇÃO. A decomposição devolve o eixo, não a orientação;
   trocar o sinal dá a mesma solução. Fixamos pela comunidade #0 ficar do lado
   negativo, para que duas execuções da mesma janela não saiam espelhadas. Isso
   torna o sinal comparável DENTRO de uma campanha e não fora dela — é por isso
   que `method` vai gravado junto com o escore.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections import Counter
from typing import Any, Iterable

from .db import utcnow
from .graph import GRAPH_VERSION, LEIDEN_SEED, VIEWS, edge_scope_of

METHOD = "ca-amp-v1"
AXIS = "amp"

# Forma única do diagnóstico. Definida num lugar só porque os três caminhos de
# saída precisam das MESMAS chaves: quem consome não pode ter que descobrir
# quais existem em qual caso.
DIAG_VAZIO = {"convergiu": False, "sigma1": 0.0, "inercia": 0.0,
              "inercia_total": 0.0, "fatia_inercia": 0.0,
              "iteracoes": 0, "residuo": None, "dim": 0}
VAZIO_EXTRA = {"componentes": 0, "cobertura_componente": 0.0,
               "fora_do_componente": 0,
               "decis": [], "fatia_no_meio": 0.0, "com_comunidade": 0,
               "atores_na_particao": 0, "amplificadores": 0, "amplificados": 0,
               "concordancia": None, "n_comparados": 0, "maiores": []}

# Abaixo disto o ator não escolheu: amplificou uma conta só.
MIN_ALVOS = 2
# Alvo amplificado por uma pessoa só não separa ninguém de ninguém.
MIN_AMPLIFICADORES = 2
MAX_ITER = 400
# Tolerância em norma do MÁXIMO, não da soma. Com soma de valores absolutos
# sobre 1.490 componentes, 1e-10 exige ~7e-14 por componente — abaixo do ruído
# do float64 para entradas da ordem de 1/√1490. O critério era inatingível por
# construção e a iteração sempre batia no teto, informando "300 iterações" tanto
# para quem convergiu quanto para quem desistiu.
TOL = 1e-9


def _matriz(conn: sqlite3.Connection, window_start: str, scope: str,
            view: str = "amp") -> dict[tuple[int, int], float]:
    """Arestas de amplificação da janela, já ponderadas pela visão."""
    pesos = VIEWS[view]
    marcas = ",".join("?" * len(pesos))
    bruto: dict[tuple[int, int], float] = {}
    for r in conn.execute(
        f"SELECT src_actor_id, dst_actor_id, kind, weight FROM edge_window "
        f"WHERE window_start = ? AND scope = ? AND kind IN ({marcas})",
        (window_start, scope, *pesos.keys()),
    ):
        chave = (r["src_actor_id"], r["dst_actor_id"])
        bruto[chave] = bruto.get(chave, 0.0) + r["weight"] * pesos[r["kind"]]
    return bruto


def _podar(matriz: dict[tuple[int, int], float]) -> dict[tuple[int, int], float]:
    """Remove linhas e colunas sem poder de separação, até estabilizar.

    Remover um alvo pode deixar um amplificador com um alvo só, e remover esse
    amplificador pode deixar outro alvo com um amplificador só. Uma passada não
    basta — é o mesmo ponto fixo da poda do núcleo.
    """
    atual = dict(matriz)
    while atual:
        alvos: dict[int, int] = {}
        fontes: dict[int, int] = {}
        for (i, j) in atual:
            fontes[i] = fontes.get(i, 0) + 1
            alvos[j] = alvos.get(j, 0) + 1
        sobra = {k: v for k, v in atual.items()
                 if fontes[k[0]] >= MIN_ALVOS and alvos[k[1]] >= MIN_AMPLIFICADORES}
        if len(sobra) == len(atual):
            return atual
        atual = sobra
    return atual


def _maior_componente(matriz: dict[tuple[int, int], float]
                      ) -> tuple[dict[tuple[int, int], float], dict[str, int]]:
    """Restringe a matriz ao maior componente conexo do grafo bipartido.

    ISTO NÃO É LIMPEZA, É PRÉ-CONDIÇÃO. Numa análise de correspondência sobre
    tabela desconexa as primeiras dimensões apenas ENUMERAM os componentes:
    σ₁ = 1,000000 exato, e o escore de cada linha diz em qual bloco ela está,
    não onde ela está. Com mais de dois componentes o autovalor 1 tem
    multiplicidade alta, a iteração de potência não tem direção única para
    convergir, e o vetor devolvido é um ponto arbitrário do autoespaço.

    Foi o que aconteceu na pauta CPMI: σ₁ = 1,0000, 400 iterações sem
    convergir, e todos os 15.811 atores com escore ≈ −0,000214. O número
    parecia uma posição e era ruído de uma conta indefinida.

    O grafo de ATORES estava conexo (100% num componente); o bipartido
    amplificador × alvo, depois da poda de quem não teve escolha, não estava.
    São coisas diferentes e só a segunda importa para o eixo.
    """
    if not matriz:
        return {}, {"componentes": 0, "cobertura": 0.0, "fora": 0}
    viz: dict[tuple[str, int], set] = {}
    for (i, j) in matriz:
        viz.setdefault(("l", i), set()).add(("c", j))
        viz.setdefault(("c", j), set()).add(("l", i))
    vistos: set = set()
    componentes: list[set] = []
    for semente in viz:
        if semente in vistos:
            continue
        pilha, grupo = [semente], set()
        while pilha:
            atual = pilha.pop()
            if atual in grupo:
                continue
            grupo.add(atual)
            pilha.extend(viz[atual] - grupo)
        vistos |= grupo
        componentes.append(grupo)
    maior = max(componentes, key=len)
    linhas = {x[1] for x in maior if x[0] == "l"}
    recorte = {k: v for k, v in matriz.items() if k[0] in linhas}
    todas = {i for i, _ in matriz}
    return recorte, {
        "componentes": len(componentes),
        "cobertura": len(linhas) / len(todas) if todas else 0.0,
        "fora": len(todas) - len(linhas),
    }


def _dimensoes(matriz: dict[tuple[int, int], float], dims: int = 2,
               seed: int = LEIDEN_SEED
               ) -> list[tuple[dict[int, float], dict[int, float], dict[str, Any]]]:
    """As `dims` primeiras dimensões não triviais da análise de correspondência.

    Cada item é (escores de quem amplifica, escores de quem é amplificado,
    diagnóstico). O diagnóstico traz `convergiu` porque sem ele não há como
    distinguir uma solução estável de uma que bateu no teto de iterações — e as
    duas viram o mesmo número no relatório.

    A matriz normalizada A = Dr^-1/2 · P · Dc^-1/2 tem um par singular trivial
    conhecido de antemão — (√r, √c) com σ = 1 — que corresponde à independência
    entre linha e coluna e não carrega informação nenhuma. Por isso cada passo
    projeta o vetor trivial fora: sem a deflação a iteração converge para o
    resultado que já sabíamos.

    POR QUE DUAS DIMENSÕES E NÃO UMA. No dado real a primeira dimensão
    degenerou: σ₁ = 0,993 e as comunidades #1 a #7 caíram TODAS em +0,285 ±
    0,003, contra −0,954 da #0. Com um bloco quase desconexo, a primeira
    dimensão vira o indicador daquele bloco — um teste de "é a #0 ou não" — e
    não um eixo de posições. Quando isso acontece, o posicionamento que sobra
    está na segunda, e sem calculá-la a seção não tem o que mostrar.

    As coordenadas de LINHA e de COLUNA vêm as duas: numa rede de amplificação
    quem tem PageRank alto é o AMPLIFICADO, isto é, uma coluna. A primeira
    versão devolvia só linhas e a tabela de atores saiu com a posição vazia em
    11 dos 12 perfis. Na AC as duas coordenadas principais vivem no mesmo
    espaço e têm a mesma variância na dimensão, o que é o que torna isto um
    biplot em vez de dois gráficos sobrepostos.
    """
    linhas = sorted({i for i, _ in matriz})
    colunas = sorted({j for _, j in matriz})
    li = {a: k for k, a in enumerate(linhas)}
    ci = {a: k for k, a in enumerate(colunas)}
    nl, nc = len(linhas), len(colunas)
    vazio = dict(DIAG_VAZIO)
    if nl < 2 or nc < 2:
        return [({}, {}, vazio)]

    total = sum(matriz.values())
    r = [0.0] * nl
    c = [0.0] * nc
    for (i, j), w in matriz.items():
        r[li[i]] += w / total
        c[ci[j]] += w / total

    # A_ij pré-calculado: a iteração toca cada aresta duas vezes por passo e
    # recomputar a raiz aqui dentro dobraria o custo.
    a_ij = [(li[i], ci[j], (w / total) / math.sqrt(r[li[i]] * c[ci[j]]))
            for (i, j), w in matriz.items()]
    raiz_c = [math.sqrt(x) for x in c]
    # Inércia total = χ²/N = Σ a_ij² − 1.
    #
    # ATENÇÃO ao usar isto como denominador. Em tabela esparsa — e esta é
    # esparsíssima: 20 mil amplificadores, mil e poucos alvos, dois alvos por
    # amplificador — a inércia total é dominada pelo número de células vazias,
    # não pela estrutura. Medido: duas metades PERFEITAMENTE separadas, com
    # σ₁ = 1,000, dão 0,5% da inércia. A "% da inércia explicada" que faz
    # sentido numa tabela de contingência densa aqui não mede nada.
    #
    # O número interpretável é σ₁ sozinho: é a correlação canônica entre o
    # escore de quem amplifica e o escore de quem é amplificado. σ₁ = 0,99 quer
    # dizer que saber a posição de um prevê quase exatamente a do outro.
    inercia_total = sum(a * a for _, _, a in a_ij) - 1.0

    def a_vec(v: list[float]) -> list[float]:
        out = [0.0] * nl
        for i, j, a in a_ij:
            out[i] += a * v[j]
        return out

    def at_vec(u: list[float]) -> list[float]:
        out = [0.0] * nc
        for i, j, a in a_ij:
            out[j] += a * u[i]
        return out

    achados: list[list[float]] = [raiz_c]

    def deflacionar(v: list[float]) -> None:
        """Projeta fora o vetor trivial E as dimensões já encontradas."""
        for base in achados:
            proj = sum(v[k] * base[k] for k in range(nc))
            for k in range(nc):
                v[k] -= proj * base[k]

    saida: list[tuple[dict[int, float], dict[int, float], dict[str, Any]]] = []
    rng = random.Random(seed)
    for dim in range(1, dims + 1):
        v = [rng.uniform(-1.0, 1.0) for _ in range(nc)]
        deflacionar(v)
        norma = math.sqrt(sum(x * x for x in v)) or 1.0
        v = [x / norma for x in v]

        sigma2 = 0.0
        gastas = 0
        residuo = float("inf")
        convergiu = False
        for gastas in range(1, MAX_ITER + 1):
            prox = at_vec(a_vec(v))
            deflacionar(prox)
            sigma2 = math.sqrt(sum(x * x for x in prox))
            if sigma2 <= 0:
                break
            prox = [x / sigma2 for x in prox]
            residuo = max(abs(prox[k] - v[k]) for k in range(nc))
            v = prox
            if residuo < TOL:
                convergiu = True
                break
        if sigma2 <= 0:
            break

        u = a_vec(v)
        norma_u = math.sqrt(sum(x * x for x in u))
        if norma_u <= 0:
            break
        sigma = math.sqrt(sigma2)
        diag = {"dim": dim, "convergiu": convergiu, "sigma1": sigma, "inercia": sigma2,
                "inercia_total": inercia_total,
                "fatia_inercia": (sigma2 / inercia_total) if inercia_total > 0 else 0.0,
                "iteracoes": gastas, "residuo": residuo}
        saida.append((
            {linhas[k]: sigma * (u[k] / norma_u) / math.sqrt(r[k]) for k in range(nl)},
            {colunas[k]: sigma * v[k] / math.sqrt(c[k]) for k in range(nc)},
            diag))
        achados.append(v)
    if not saida:
        return [({}, {}, {**vazio, "iteracoes": 0})]
    return saida


def _orientar(escores: dict[int, float], comunidade: dict[int, int]) -> dict[int, float]:
    """Fixa o sinal do eixo pela comunidade #0 ficar do lado negativo.

    A decomposição devolve um eixo, não uma orientação: -v é tão solução quanto
    v. Sem uma regra, a mesma janela sai espelhada entre execuções e a coluna
    "Δ posição" vira ruído puro.
    """
    membros = [escores[a] for a in escores if comunidade.get(a) == 0]
    if membros and sum(membros) > 0:
        return {a: -s for a, s in escores.items()}
    return escores


def _normalizar(escores: dict[int, float]) -> dict[int, float]:
    """Reescala para [-1, +1] pelos extremos, que viram as âncoras do eixo."""
    maior = max((abs(s) for s in escores.values()), default=0.0)
    if maior <= 0:
        return escores
    return {a: s / maior for a, s in escores.items()}


def _concordancia(escores: dict[int, float],
                  comunidade: dict[int, int]) -> dict[str, Any]:
    """O eixo é um achado, ou é a partição do Leiden repintada?

    Esta é a pergunta que decide se a seção vale existir. Se o sinal do escore
    prevê a comunidade com 99% de acerto, o "eixo de posicionamento" não
    acrescenta nada ao que a detecção de comunidade já tinha dito — e apresentá-lo
    como uma medida independente seria vender duas vezes o mesmo achado.

    Compara só as DUAS maiores comunidades: com 23 comunidades, a pergunta
    "o eixo separa os grupos?" só é respondível entre os que têm tamanho.
    """
    tamanhos: Counter = Counter(comunidade[a] for a in escores if a in comunidade)
    if len(tamanhos) < 2:
        return {"concordancia": None, "n_comparados": 0, "maiores": []}
    (c1, n1), (c2, n2) = tamanhos.most_common(2)
    acertos = 0
    lados: dict[int, dict[str, int]] = {c1: {"neg": 0, "pos": 0},
                                        c2: {"neg": 0, "pos": 0}}
    for a, s in escores.items():
        cid = comunidade.get(a)
        if cid in lados:
            lados[cid]["neg" if s < 0 else "pos"] += 1
    # a melhor das duas atribuições de sinal: o rótulo em si é convenção
    acertos = max(lados[c1]["neg"] + lados[c2]["pos"],
                  lados[c1]["pos"] + lados[c2]["neg"])
    total = n1 + n2
    return {
        "concordancia": acertos / total if total else None,
        "n_comparados": total,
        "maiores": [{"id": c1, "n": n1, **lados[c1]},
                    {"id": c2, "n": n2, **lados[c2]}],
    }


def compute_positions(
    conn: sqlite3.Connection, window_start: str, scope: str,
    view: str = "amp", graph_version: int = GRAPH_VERSION,
    edge_scope: str | None = None, persist: bool = True, dims: int = 2,
) -> dict[str, Any]:
    """Calcula e grava o eixo de posicionamento de uma janela e escopo.

    `scope` é o escopo ANALÍTICO (onde estão as comunidades, ex.
    `amp:topic:Yanomami:core`); `edge_scope` é onde estão as arestas
    (`topic:Yanomami`). Quando não informado, deriva-se do primeiro.
    """
    if edge_scope is None:
        edge_scope = edge_scope_of(scope)

    bruto = _matriz(conn, window_start, edge_scope, view)
    matriz, comp = _maior_componente(_podar(bruto))
    if not matriz:
        vazio = {"scope": scope, "eixo": AXIS, "atores": 0, "alvos": 0,
                 "descartados": len({i for i, _ in bruto}),
                 **VAZIO_EXTRA, **DIAG_VAZIO,
                 "componentes": comp["componentes"]}
        return {**vazio, "dimensoes": [vazio], "degenerada": False}

    comunidade = {
        r["actor_id"]: r["community_id"] for r in conn.execute(
            "SELECT actor_id, community_id FROM actor_community "
            "WHERE window_start = ? AND scope = ? AND graph_version = ?",
            (window_start, scope, graph_version))
    }
    dimensoes = _dimensoes(matriz, dims=dims)
    resultados: list[dict[str, Any]] = []
    if persist:
        conn.execute(
            "DELETE FROM actor_position WHERE window_start = ? AND scope = ? "
            "AND axis LIKE ?", (window_start, scope, AXIS + "%"))
    agora = utcnow()

    for por_linha, por_coluna, diag in dimensoes:
        # Quem é amplificado tem a posição estimada a partir de MUITOS
        # amplificadores; quem amplifica, a partir de poucos alvos. Quando o
        # mesmo ator aparece nos dois papéis, a de coluna é a melhor das duas.
        escores = {**por_linha, **por_coluna}
        papel = {a: ("amplificado" if a in por_coluna else "amplifica")
                 for a in escores}
        if not escores:
            resultados.append({"scope": scope, "atores": 0, "alvos": 0,
                               "descartados": len({i for i, _ in bruto}),
                               **VAZIO_EXTRA,
                               "atores_na_particao": len(comunidade), **diag})
            continue
        escores = _normalizar(_orientar(escores, comunidade))
        extremos = sorted(escores, key=lambda a: escores[a])
        ancoras = {extremos[0], extremos[-1]}
        eixo = AXIS if diag["dim"] == 1 else f"{AXIS}{diag['dim']}"

        if persist:
            conn.executemany(
                "INSERT INTO actor_position (actor_id, axis, window_start, scope, "
                "score, method, is_anchor, computed_at) VALUES (?,?,?,?,?,?,?,?)",
                [(a, eixo, window_start, scope, s,
                  f"{METHOD}/{papel[a]}", 1 if a in ancoras else 0, agora)
                 for a, s in escores.items()])
        resultados.append(_resumo(escores, comunidade, matriz, bruto, por_linha,
                                  por_coluna, ancoras, scope, eixo, diag))

    principal = resultados[0]
    principal["dimensoes"] = resultados
    principal["componentes"] = comp["componentes"]
    principal["cobertura_componente"] = comp["cobertura"]
    principal["fora_do_componente"] = comp["fora"]
    # A dimensão 1 vira indicador de bloco quando há um grupo quase desconexo:
    # concordância ~100% com a partição e quase ninguém no meio. Quando isso
    # acontece o posicionamento útil está na 2.
    principal["degenerada"] = bool(
        (principal.get("concordancia") or 0) > 0.95
        and principal.get("fatia_no_meio", 1.0) < 0.05)
    return principal


def _resumo(escores, comunidade, matriz, bruto, por_linha, por_coluna,
            ancoras, scope, eixo, diag) -> dict[str, Any]:

    """Os números de UMA dimensão, prontos para o relatório.

    A distribuição entra porque um eixo com σ₁ perto de 1 tende a virar
    indicador binário do bloco em vez de um contínuo de posições, e o relatório
    precisa saber em qual dos dois casos está antes de chamar isto de "eixo".
    """
    ordenados = sorted(escores.values())
    n = len(ordenados)
    decis = [ordenados[min(n - 1, (n * k) // 10)] for k in range(11)]
    meio = sum(1 for s in ordenados if abs(s) < 0.25)

    with_com = sum(1 for a in escores if a in comunidade)
    return {
        "scope": scope, "eixo": eixo, "atores": len(escores),
        "alvos": len({j for _, j in matriz}),
        "descartados": len({i for i, _ in bruto}) - len(escores),
        "ancoras": sorted(ancoras),
        "decis": decis, "fatia_no_meio": meio / n if n else 0.0,
        # cobertura: o eixo sai do grafo cheio da pauta, a partição sai do
        # núcleo. São filtros diferentes, então a interseção precisa aparecer
        # antes que alguém leia "eixo médio da comunidade" como cobrindo todos.
        "com_comunidade": with_com,
        "atores_na_particao": len(comunidade),
        "amplificadores": len(por_linha), "amplificados": len(por_coluna),
        **_concordancia(escores, comunidade),
        **diag,
    }


def positions_of(conn: sqlite3.Connection, window_start: str, scope: str,
                 actor_ids: Iterable[int] | None = None,
                 axis: str = AXIS) -> dict[int, float]:
    """Escores já gravados, para juntar a uma tabela de atores."""
    linhas = conn.execute(
        "SELECT actor_id, score FROM actor_position "
        "WHERE window_start = ? AND scope = ? AND axis = ?",
        (window_start, scope, axis)).fetchall()
    tudo = {r["actor_id"]: r["score"] for r in linhas}
    if actor_ids is None:
        return tudo
    alvo = set(actor_ids)
    return {a: s for a, s in tudo.items() if a in alvo}
